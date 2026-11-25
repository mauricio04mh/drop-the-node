"""Repair agent: restores lost replicas and collects objects nobody uses.

Anti-entropy by comparing inventories. Each round:

1. Survey: ask every store for its inventory and for all its manifests and
   tombstones (``search("")``; the empty query matches every manifest).
2. Mark: every object referenced by a manifest that is not withdrawn (its
   segments, its initial segment and its original) is in use.
3. Restore: an object with fewer than N replicas is copied, from a replica
   whose hash checks out, to the first stores of its placement order that
   lack it. Replicas count on the stores that answered and, for
   ``DTN_STORE_LOST_SECONDS``, on silent ones (see ``Repairer``).
   Tombstones go to every store.
4. Sweep: objects that are not in use are deleted with ``delete_if_older``
   once older than the grace period. Manifests and tombstones are never
   deleted, so a withdrawal stays visible.

Two rules keep the restore and the sweep from undoing each other:

- The sweep runs only if every store answered. A manifest whose only replica
  sits on a silent store is unknown, and its segments would look unused.
- With every store answered, unused objects are not restored. Otherwise
  each round would bring back, with a fresh age, copies the sweep deleted,
  and garbage would never go away.

Writing an immutable object is idempotent, so a round can overlap uploads or
another agent without coordination. The agent is not on the read path: if it
dies, playback goes on and only repair is delayed.

``Repairer`` holds the logic and runs in any process with a ``RayCluster``.
``RepairActor`` runs it periodically as a restartable Ray actor.
"""

import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

import ray

from dtn.config import StorageConfig
from dtn.errors import NotFound, Unavailable
from dtn.placement import placement_order
from dtn.ray_cluster import RayCluster

log = logging.getLogger("dtn.repair")

ACTOR_NAME = "repair"
UNLIMITED_RESTARTS = -1


@dataclass
class RoundReport:
    """What one round saw and did. Logged and returned (principle P7)."""

    op_id: str
    reason: str
    unreachable: tuple[str, ...] = ()
    # Silent stores whose last inventory still counts (not lost yet), and
    # silent stores given up as lost.
    presumed: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    objects: int = 0
    copies_made: int = 0
    copies_failed: int = 0
    unreadable: list[str] = field(default_factory=list)
    deleted: int = 0
    kept_recent: int = 0
    sweep_skipped: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Survey:
    answered: tuple[str, ...]
    unreachable: tuple[str, ...]
    holders: dict  # object id -> {store: inventory entry}
    kinds: dict  # object id -> "manifest" | "tombstone" | "data"
    manifests: dict  # track id -> Manifest
    withdrawn_tracks: set
    withdrawn_originals: set

    @property
    def complete(self) -> bool:
        return not self.unreachable


class Repairer:
    """Holds state across rounds: the last inventory of each store and since
    when each silent store has been silent.

    A store that stops answering is not given up at once. Until it has been
    silent for ``DTN_STORE_LOST_SECONDS``, the replicas of its last inventory
    still count, so a store that restarts or comes back after a short outage
    causes no copies. After that it is lost, and its objects are restored on
    the next stores. Two cases are restored at once, the safe direction,
    because nothing is known of them: objects written to the silent store
    after its last inventory, and every object of a store this agent never
    saw answering (it was already silent when the agent started).
    """

    def __init__(self, cluster: RayCluster, config: StorageConfig, *, clock=time.monotonic):
        self._cluster = cluster
        self._replicas = config.replicas
        self._grace = config.gc_grace_seconds
        self._lost_after = config.store_lost_seconds
        self._clock = clock
        self._last_inventory: dict[str, set] = {}
        self._silent_since: dict[str, float] = {}
        self._given_up: set[str] = set()
        # Reading the source replica: one that takes longer than a whole
        # period is abandoned and retried in the next round. Each write gets
        # the size-based timeout of RayCluster.write_timeout instead.
        self._read_timeout = config.repair_interval_seconds

    def run_round(self, *, reason: str = "requested", op_id: str | None = None) -> RoundReport:
        op_id = op_id or f"repair-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        survey = self._survey(op_id)
        presumed = self._track_silence(survey)
        report = RoundReport(
            op_id,
            reason,
            unreachable=survey.unreachable,
            presumed=tuple(sorted(presumed)),
            lost=tuple(s for s in survey.unreachable if s not in presumed),
            objects=len(survey.holders),
        )

        in_use = self._mark(survey)
        self._restore(survey, in_use, presumed, report)
        if survey.complete:
            self._sweep(survey, in_use, report)
        else:
            report.sweep_skipped = f"no answer from {', '.join(survey.unreachable)}"

        report.seconds = time.monotonic() - started
        log.info("op=%s round %s", op_id, report.as_dict())
        return report

    def loss_pending(self) -> bool:
        """True once a silent store has been silent long enough to be lost
        and no round has acted on it yet."""
        now = self._clock()
        return any(
            now - since >= self._lost_after
            for store, since in self._silent_since.items()
            if store not in self._given_up
        )

    # ------------------------------------------------------------------

    def _track_silence(self, survey: "_Survey") -> dict:
        """Update silence bookkeeping; return {store: last inventory ids} for
        the silent stores whose replicas still count."""
        now = self._clock()
        for store in survey.answered:
            self._last_inventory[store] = {
                obj_id for obj_id, holding in survey.holders.items() if store in holding
            }
            self._silent_since.pop(store, None)
            self._given_up.discard(store)
        presumed = {}
        for store in survey.unreachable:
            since = self._silent_since.setdefault(store, now)
            known = self._last_inventory.get(store)
            if known is not None and now - since < self._lost_after:
                presumed[store] = known
            else:
                self._given_up.add(store)
        return presumed

    def _survey(self, op_id: str) -> _Survey:
        deadline = time.monotonic() + self._cluster.detector.timeout_cap
        # Inventory first: any manifest it lists is already indexed, so the
        # search that follows returns it too.
        inventories = self._cluster.scatter("inventory", deadline=deadline, op_id=op_id)
        searches = self._cluster.scatter("search", "", deadline=deadline, op_id=op_id)
        answered = tuple(
            s for s in self._cluster.stores if s in inventories.responses and s in searches.responses
        )
        unreachable = tuple(s for s in self._cluster.stores if s not in answered)

        holders: dict = {}
        kinds: dict = {}
        manifests: dict = {}
        withdrawn_tracks: set = set()
        withdrawn_originals: set = set()
        for store in answered:
            for entry in inventories.responses[store]:
                holders.setdefault(entry["id"], {})[store] = entry
                kinds[entry["id"]] = entry["kind"]
            found = searches.responses[store]
            for item in found["manifests"]:
                manifests[item["track_id"]] = item["manifest"]
            for t in found["tombstoned"]:
                withdrawn_tracks.add(t["track_id"])
                withdrawn_originals.add(t["original"])
        return _Survey(
            answered, unreachable, holders, kinds, manifests, withdrawn_tracks, withdrawn_originals
        )

    @staticmethod
    def _mark(survey: _Survey) -> set:
        """Ids of the objects that tracks still in force refer to."""
        in_use = set()
        for track_id, manifest in survey.manifests.items():
            if track_id in survey.withdrawn_tracks or manifest.original in survey.withdrawn_originals:
                continue  # withdrawn: its audio is no longer needed
            in_use.add(manifest.init)
            in_use.add(manifest.original)
            in_use.update(segment.id for segment in manifest.segments)
        return in_use

    def _needs_replicas(self, survey: _Survey, obj_id: str, in_use: set) -> bool:
        if survey.kinds[obj_id] != "data":
            return True  # manifests and tombstones are always kept
        # With a store silent, an object may belong to a manifest only that
        # store holds: keep its replicas, as if it were in use.
        return obj_id in in_use or not survey.complete

    def _restore(self, survey: _Survey, in_use: set, presumed: dict, report: RoundReport) -> None:
        for obj_id, holding in survey.holders.items():
            if not self._needs_replicas(survey, obj_id, in_use):
                continue
            # Replicas on a silent store that is not lost yet still count.
            replicas = len(holding) + sum(obj_id in ids for ids in presumed.values())
            lacking = [
                s
                for s in placement_order(obj_id, self._cluster.stores)
                if s in survey.answered and s not in holding
            ]
            if survey.kinds[obj_id] == "tombstone":
                targets = lacking  # every store, so any one can show the withdrawal
            else:
                targets = lacking[: max(0, self._replicas - replicas)]
            if targets:
                self._copy(obj_id, targets, report)

    def _copy(self, obj_id: str, targets: list, report: RoundReport) -> None:
        op_id = report.op_id
        try:
            # Reads in placement order and checks the hash, like any client.
            data = self._cluster.get(obj_id, deadline=time.monotonic() + self._read_timeout, op_id=op_id)
        except (NotFound, Unavailable) as e:
            # Every copy the stores offered was damaged or vanished meanwhile.
            report.unreadable.append(obj_id)
            log.error("op=%s cannot read any replica of %s: %s", op_id, obj_id, e)
            return
        for store in targets:
            kind, _ = self._cluster.call(
                store,
                "put",
                obj_id,
                data,
                deadline=time.monotonic() + self._cluster.write_timeout(store, len(data)),
                op_id=op_id,
            )
            if kind == "ok":
                report.copies_made += 1
                log.info("op=%s copied %s to %s", op_id, obj_id, store)
            else:
                # Saturated stores included: repair yields to clients and
                # comes back in the next round.
                report.copies_failed += 1
                log.warning("op=%s could not copy %s to %s (%s)", op_id, obj_id, store, kind)

    def _sweep(self, survey: _Survey, in_use: set, report: RoundReport) -> None:
        for obj_id, holding in survey.holders.items():
            if survey.kinds[obj_id] != "data" or obj_id in in_use:
                continue
            for store, entry in holding.items():
                if entry["age_seconds"] <= self._grace:
                    report.kept_recent += 1
                    continue
                # The store checks the age again, under its lock: a write
                # since the inventory keeps the object.
                kind, value = self._cluster.call(
                    store,
                    "delete_if_older",
                    obj_id,
                    self._grace,
                    deadline=time.monotonic() + self._cluster.detector.timeout_cap,
                    op_id=report.op_id,
                )
                if kind == "ok" and value["deleted"]:
                    report.deleted += 1
                elif kind == "ok" and value["reason"] == "recent":
                    report.kept_recent += 1


@ray.remote
class RepairActor:
    """Runs a round every ``DTN_REPAIR_INTERVAL_SECONDS``, at once when a
    store stops answering or comes back (a new incarnation), and when a
    silent store has been silent for ``DTN_STORE_LOST_SECONDS``."""

    def __init__(self, config: StorageConfig, namespace: str):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        self._cluster = RayCluster.connect(config, namespace=namespace)
        self._repairer = Repairer(self._cluster, config)
        self._round_lock = threading.Lock()
        self._last: RoundReport | None = None
        self._stop = threading.Event()
        # Watch the stores as often as a suspect store is probed.
        watch_every = config.timeout_max_ms / 1000
        self._loop = threading.Thread(
            target=self._run,
            args=(config.repair_interval_seconds, watch_every),
            name="repair-loop",
            daemon=True,
        )
        self._loop.start()
        log.info("repair agent started pid=%d config=%s", os.getpid(), config.as_dict())

    def run_now(self) -> RoundReport:
        """Run a round at once and return its report."""
        return self._round("requested")

    def last_report(self) -> RoundReport | None:
        return self._last

    def process_id(self) -> int:
        return os.getpid()

    def _round(self, reason: str) -> RoundReport:
        with self._round_lock:
            self._last = self._repairer.run_round(reason=reason)
            return self._last

    def _membership(self) -> frozenset:
        """(store, incarnation) of every store that answers a ping now."""
        pings = self._cluster.scatter(
            "ping",
            deadline=time.monotonic() + self._cluster.detector.timeout_cap,
            op_id="repair-watch",
        )
        return frozenset((store, reply["incarnation"]) for store, reply in pings.responses.items())

    def _run(self, interval: float, watch_every: float) -> None:
        next_due = time.monotonic()  # first round at once
        seen = None
        while True:
            try:
                current = self._membership()
                if seen is not None and current != seen:
                    self._round("membership")
                    next_due = time.monotonic() + interval
                elif self._repairer.loss_pending():
                    self._round("store-lost")
                    next_due = time.monotonic() + interval
                elif time.monotonic() >= next_due:
                    self._round("periodic")
                    next_due = time.monotonic() + interval
                seen = current
            except Exception:  # keep repairing whatever one round hit
                log.exception("repair round failed")
            if self._stop.wait(watch_every):
                return


def repair_options(namespace: str) -> dict:
    """Ray options for the repair agent: restarted like the stores."""
    return {
        "name": ACTOR_NAME,
        "namespace": namespace,
        "lifetime": "detached",
        "max_restarts": UNLIMITED_RESTARTS,
        "max_task_retries": 0,
        # The loop runs in its own thread; two more serve run_now and
        # last_report while it works.
        "max_concurrency": 2,
        "num_cpus": 0,
    }
