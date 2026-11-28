"""``RayCluster``: the ``Cluster`` interface over the store actors.

Everything that makes storage fault tolerant lives here, on the client side:
choosing replicas, noticing failures, hedging slow reads, retrying saturated
stores and deciding between ``NotFound`` and ``Unavailable``. Ray only
carries the calls and reports dead actors; none of these decisions is Ray's.

Methods are safe to call from several threads at once (an upload writes
segments in parallel): per-call state is local, and the failure detector
has its own lock.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass

import ray
from ray.exceptions import RayActorError

from dtn.cluster import Scattered
from dtn.config import StorageConfig, check_cluster_params
from dtn.detector import FailureDetector
from dtn.errors import Corrupt, Invalid, NotFound, Unavailable
from dtn.ids import require_id, sha256_hex
from dtn.placement import placement_order

log = logging.getLogger("dtn.cluster")

# Retry of a saturated store during a write: exponential, with jitter.
BACKOFF_FIRST = 0.02
BACKOFF_MAX = 1.0

SCATTER_METHODS = frozenset({"search", "tombstones", "inventory", "ping"})
# Requests the repair agent sends to one chosen store.
TARGETED_METHODS = frozenset({"put", "delete_if_older"})


@dataclass(frozen=True)
class ReadTrace:
    """Who delivered an object, and why it was not the preferred store.

    ``reason`` is None when the preferred store (first in placement order)
    answered; otherwise "suspect" (it was skipped), "error" (it failed or did
    not have the object), "corrupt" (its copy was damaged) or "hedged" (the
    duplicate request to another store answered first).
    """

    store: str
    preferred: str
    reason: str | None


class RayCluster:
    def __init__(self, handles: dict, config: StorageConfig, *, probe_interval: float | None = None):
        self.stores = tuple(config.store_names)
        self.replicas = config.replicas
        self._handles = handles
        self._min_write_rate = config.min_write_mbps * 1_000_000  # bytes per second
        self.detector = FailureDetector(
            self.stores,
            timeout_min=config.timeout_min_ms / 1000,
            timeout_max=config.timeout_max_ms / 1000,
            hedge_delay=None if config.hedge_delay_ms is None else config.hedge_delay_ms / 1000,
        )
        # For each suspect store, an object it was asked for when it fell under
        # suspicion. Probing with a real read of it, not just a ping, keeps a
        # store whose disk is slow from being readmitted while still slow.
        self._probe_ids: dict[str, str] = {}
        self._stop = threading.Event()
        self._prober = threading.Thread(
            target=self._probe_suspects,
            args=(probe_interval if probe_interval is not None else config.timeout_max_ms / 1000,),
            name="probe-suspects",
            daemon=True,
        )
        self._prober.start()

    @classmethod
    def connect(cls, config: StorageConfig, *, namespace: str | None = None) -> "RayCluster":
        """Attach to stores started with ``dtn.admin``.

        Raises ``ValueError`` if a store that answers runs with another S or N:
        this process would place, count and repair replicas differently.
        """
        from dtn import admin

        cluster = cls(admin.get_stores(config, namespace=namespace or admin.NAMESPACE), config)
        pings = cluster.scatter(
            "ping", deadline=time.monotonic() + cluster.detector.timeout_cap, op_id="connect"
        )
        try:
            check_cluster_params(config, pings.responses)
        except ValueError:
            cluster.close()
            raise
        return cluster

    def close(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Writes

    def put(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        self._check_object(obj_id, data, op_id)
        candidates = self.detector.prefer(placement_order(obj_id, self.stores))
        confirmed: set[str] = set()
        pending: dict = {}  # ref -> (store, attempt, time its write timeout runs out)
        late: set = set()  # refs past their write timeout, already replaced
        retry_at: dict[str, tuple[float, int]] = {}
        next_i = 0

        def launch(store: str, attempt: int) -> None:
            give_up = time.monotonic() + self.write_timeout(store, len(data))
            ref = self._handles[store].put.remote(obj_id, data, op_id=op_id)
            pending[ref] = (store, attempt, give_up)

        def launch_next() -> None:
            nonlocal next_i
            if next_i < len(candidates):
                launch(candidates[next_i], 0)
                next_i += 1

        for _ in range(min(self.replicas, len(candidates))):
            launch_next()

        while len(confirmed) < self.replicas:
            now = time.monotonic()
            if now >= deadline:
                self._cancel(pending)
                raise Unavailable(
                    f"deadline passed with {len(confirmed)} of {self.replicas} replicas of {obj_id}",
                    op_id=op_id,
                )
            for store, (when, attempt) in list(retry_at.items()):
                if when <= now:
                    del retry_at[store]
                    launch(store, attempt)
            overdue = [r for r, (_, _, give_up) in pending.items() if r not in late and now >= give_up]
            for ref in overdue:
                # Alive or not, it is too slow for its size: write the replica
                # on the next store too. The late write keeps running and still
                # counts if it finishes first.
                late.add(ref)
                self._suspect(pending[ref][0], "write timeout", op_id)
                launch_next()
            if not pending and not retry_at:
                if next_i < len(candidates):
                    launch_next()
                    continue
                raise Unavailable(
                    f"only {len(confirmed)} of {self.replicas} replicas of {obj_id} confirmed",
                    op_id=op_id,
                )

            wake = min(
                [deadline]
                + [when for when, _ in retry_at.values()]
                + [give_up for r, (_, _, give_up) in pending.items() if r not in late]
            )
            if not pending:
                time.sleep(max(0.0, wake - now))
                continue
            ready, _ = ray.wait(list(pending), num_returns=1, timeout=max(0.0, wake - now))
            for ref in ready:
                store, attempt, _ = pending.pop(ref)
                was_late = ref in late
                late.discard(ref)
                kind, _ = self._outcome(ref, op_id)
                if kind == "ok":
                    confirmed.add(store)
                elif was_late:
                    continue  # already replaced by the next store
                elif kind == "saturated":
                    # Alive and about to have room: retry the same store so
                    # the replica lands where readers look first.
                    retry_at[store] = (time.monotonic() + _backoff(attempt), attempt + 1)
                else:
                    self._suspect(store, kind, op_id, obj_id)
                    launch_next()

    def write_timeout(self, store: str, size: int) -> float:
        """How long writing ``size`` bytes to ``store`` may take before it is
        suspected: its adaptive timeout plus the time the disk needs at
        ``DTN_MIN_WRITE_MBPS``. A fixed timeout would suspect every store on
        large objects, or wait far too long on small ones."""
        return self.detector.timeout(store) + size / self._min_write_rate

    def put_everywhere(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        self._check_object(obj_id, data, op_id)
        confirmed: set[str] = set()
        pending: dict = {}
        retry_at: dict[str, tuple[float, int]] = {}
        grace: float | None = None

        def launch(store: str, attempt: int) -> None:
            pending[self._handles[store].put.remote(obj_id, data, op_id=op_id)] = (store, attempt)

        for store in self.stores:
            launch(store, 0)

        while pending or retry_at:
            now = time.monotonic()
            if len(confirmed) >= self.replicas and grace is None:
                # Enough for the guarantee; give the rest one timeout to catch up.
                grace = min(deadline, now + self.detector.timeout_cap)
            stop_at = grace if grace is not None else deadline
            if now >= stop_at:
                break
            for store, (when, attempt) in list(retry_at.items()):
                if when <= now:
                    del retry_at[store]
                    launch(store, attempt)
            wake = min([stop_at] + [when for when, _ in retry_at.values()])
            if not pending:
                time.sleep(max(0.0, wake - now))
                continue
            ready, _ = ray.wait(list(pending), num_returns=1, timeout=max(0.0, wake - now))
            for ref in ready:
                store, attempt = pending.pop(ref)
                kind, _ = self._outcome(ref, op_id)
                if kind == "ok":
                    confirmed.add(store)
                elif kind == "saturated":
                    retry_at[store] = (time.monotonic() + _backoff(attempt), attempt + 1)
                else:
                    self._suspect(store, kind, op_id, obj_id)

        self._cancel(pending)
        if len(confirmed) < self.replicas:
            raise Unavailable(
                f"only {len(confirmed)} of {self.replicas} copies of {obj_id} confirmed",
                op_id=op_id,
            )

    # ------------------------------------------------------------------
    # Reads

    def get(self, obj_id: str, *, deadline: float, op_id: str) -> bytes:
        data, _ = self.get_traced(obj_id, deadline=deadline, op_id=op_id)
        return data

    def get_traced(self, obj_id: str, *, deadline: float, op_id: str) -> tuple[bytes, ReadTrace]:
        """``get``, also reporting which store delivered and why (for playback events)."""
        require_id(obj_id, op_id=op_id)
        order = placement_order(obj_id, self.stores)
        preferred = order[0]
        candidates = self.detector.prefer(order)
        reason = "suspect" if candidates[0] != preferred else None
        pending: dict = {}  # ref -> (store, started)
        absent: set[str] = set()  # stores that answered "I do not have it"
        next_i = 0
        hedge_at = 0.0

        def launch() -> None:
            nonlocal next_i, hedge_at
            store = candidates[next_i]
            next_i += 1
            started = time.monotonic()
            pending[self._handles[store].get.remote(obj_id, op_id=op_id)] = (store, started)
            hedge_at = started + self.detector.hedge_delay(store)

        launch()
        while pending or next_i < len(candidates):
            now = time.monotonic()
            if now >= deadline:
                self._cancel(pending)
                raise Unavailable(f"deadline passed reading {obj_id}", op_id=op_id)
            if not pending:
                launch()
                continue
            for store, started in pending.values():
                if now - started > self.detector.timeout(store):
                    self._suspect(store, "timeout", op_id, obj_id)
            if next_i < len(candidates) and now >= hedge_at:
                launch()  # hedge: the slow request keeps running
                continue

            wake = deadline
            if next_i < len(candidates):
                wake = min(wake, hedge_at)
            for store, started in pending.values():
                if not self.detector.is_suspect(store):
                    wake = min(wake, started + self.detector.timeout(store))
            ready, _ = ray.wait(list(pending), num_returns=1, timeout=max(0.0, wake - now))
            if not ready:
                continue

            ref = ready[0]
            store, started = pending.pop(ref)
            kind, value = self._outcome(ref, op_id)
            if kind == "ok" and sha256_hex(value) != obj_id:
                kind = "corrupt"  # never trust bytes without checking them
            if kind == "ok":
                self.detector.observe(store, time.monotonic() - started)
                if self.detector.readmit(store):
                    log.info("op=%s readmitted %s after a successful read", op_id, store)
                self._cancel(pending)
                if store != preferred and reason is None:
                    reason = "hedged"
                return value, ReadTrace(store, preferred, reason)

            if kind == "absent":
                absent.add(store)
                self.detector.observe(store, time.monotonic() - started)
            elif kind != "saturated":
                self._suspect(store, kind, op_id, obj_id)
            if store == preferred and reason is None:
                reason = "corrupt" if kind == "corrupt" else "error"
            if next_i < len(candidates):
                launch()  # this one failed: ask the next store at once

        unreachable = len(self.stores) - len(absent)
        if unreachable < self.replicas:
            raise NotFound(f"no store has {obj_id}", op_id=op_id)
        raise Unavailable(
            f"{obj_id} not found in the stores that answered, but {unreachable} did not",
            op_id=op_id,
        )

    def scatter(self, method: str, *args, deadline: float, op_id: str) -> Scattered:
        if method not in SCATTER_METHODS:
            raise Invalid(f"cannot scatter {method!r}", op_id=op_id)
        pending: dict = {}
        for store in self.stores:
            started = time.monotonic()
            ref = getattr(self._handles[store], method).remote(*args, op_id=op_id)
            pending[ref] = (store, started)
        responses: dict = {}

        while pending:
            now = time.monotonic()
            if now >= deadline:
                break
            for store, started in pending.values():
                if now - started > self.detector.timeout(store):
                    self._suspect(store, "timeout", op_id)
            ready, _ = ray.wait(list(pending), num_returns=1, timeout=max(0.0, deadline - now))
            for ref in ready:
                store, started = pending.pop(ref)
                kind, value = self._outcome(ref, op_id)
                if kind == "ok":
                    responses[store] = value
                    self.detector.observe(store, time.monotonic() - started)
                elif kind not in ("saturated", "absent"):
                    self._suspect(store, kind, op_id)

        self._cancel(pending)
        unreachable = tuple(s for s in self.stores if s not in responses)
        return Scattered(responses=responses, unreachable=unreachable)

    def call(self, store: str, method: str, *args, deadline: float, op_id: str) -> tuple[str, object]:
        """One request to one chosen store, for the repair agent.

        Unlike ``put``, it never moves to another store and never retries: the
        agent decides where each copy goes and repeats what failed in its next
        round. Returns the outcome kind of ``_outcome`` with its value, or
        ("timeout", None) if the deadline passes first.
        """
        if method not in TARGETED_METHODS:
            raise Invalid(f"cannot call {method!r} on one store", op_id=op_id)
        ref = getattr(self._handles[store], method).remote(*args, op_id=op_id)
        ready, _ = ray.wait([ref], timeout=max(0.0, deadline - time.monotonic()))
        if not ready:
            self._cancel({ref: None})
            self._suspect(store, "timeout", op_id)
            return "timeout", None
        kind, value = self._outcome(ref, op_id)
        if kind in ("dead", "error", "corrupt"):
            self._suspect(store, kind, op_id)
        return kind, value

    # ------------------------------------------------------------------
    # Internals

    def _check_object(self, obj_id: str, data: bytes, op_id: str) -> None:
        require_id(obj_id, op_id=op_id)
        if sha256_hex(data) != obj_id:
            raise Invalid("data does not match its id", op_id=op_id)

    @staticmethod
    def _outcome(ref, op_id: str) -> tuple[str, object]:
        """Classify the result of a finished call to a store.

        "ok", "absent" (NotFound), "corrupt", "saturated", "dead" (the actor
        died) or "error" (anything else). ``Invalid`` means the caller sent a
        bad request, so it propagates.
        """
        try:
            return "ok", ray.get(ref)
        except Invalid:
            raise
        except NotFound:
            return "absent", None
        except Corrupt:
            return "corrupt", None
        except Unavailable as e:
            return ("saturated" if e.saturated else "error"), e
        except RayActorError as e:
            return "dead", e
        except Exception as e:  # any other failure of that store
            log.warning("op=%s unexpected store error: %r", op_id, e)
            return "error", e

    def _suspect(self, store: str, why: str, op_id: str, obj_id: str | None = None) -> None:
        if self.detector.suspect(store):
            if obj_id is not None:
                self._probe_ids[store] = obj_id
            log.info("op=%s suspect %s (%s)", op_id, store, why)

    @staticmethod
    def _cancel(pending: dict) -> None:
        for ref in pending:
            try:
                ray.cancel(ref)
            except Exception:  # already finished or gone: nothing to cancel
                pass

    def _probe_suspects(self, interval: float) -> None:
        """Readmit suspect stores that answer a probe in time.

        The probe repeats a real read when there is one to repeat. Any answer
        in time counts, even "not found" or "corrupt": the store is alive and
        its disk responds. "Saturated" counts too, since it is answered at once.
        """
        while not self._stop.wait(interval):
            for store in self.detector.suspects():
                handle = self._handles[store]
                probe_id = self._probe_ids.get(store)
                if probe_id is not None:
                    ref = handle.get.remote(probe_id, op_id="probe")
                else:
                    ref = handle.ping.remote(op_id="probe")
                try:
                    ray.get(ref, timeout=self.detector.timeout(store))
                except (NotFound, Corrupt):
                    pass
                except Unavailable as e:
                    if not e.saturated:
                        continue
                except Exception:
                    ray.cancel(ref)
                    continue
                if self.detector.readmit(store):
                    self._probe_ids.pop(store, None)
                    log.info("readmitted %s after a probe", store)


def _backoff(attempt: int) -> float:
    return min(BACKOFF_MAX, BACKOFF_FIRST * 2**attempt) * random.uniform(0.5, 1.0)
