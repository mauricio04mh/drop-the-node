"""The store as a Ray actor: a thin wrapper around ``StoreCore``.

This is the only place where a store touches Ray. What Ray provides here:

- a process per store, reachable by its logical name (``name`` + namespace);
- automatic restart after a crash (``max_restarts``), which reruns
  ``__init__``, so the store rebuilds its index from disk;
- several calls at once (``max_concurrency``). Ray queues calls beyond that
  limit instead of rejecting them, so the actor gets more threads than
  ``DTN_STORE_CONCURRENCY``: the extra ones let ``StoreCore`` reject quickly
  with ``Unavailable(saturated=True)`` instead of leaving callers queued.

Ray does not replay calls that were in flight when the store crashed
(``max_task_retries=0``): the client decides what is safe to repeat.
"""

import logging
import os
import threading
from dataclasses import dataclass

import ray

from dtn import objects
from dtn.config import StorageConfig
from dtn.store import Decode, Matches, StoreCore, ValidateQuery

# Restart a crashed store as many times as needed: a store that stays down
# only costs redundancy, and repair cannot bring back a store's disk.
UNLIMITED_RESTARTS = -1


@dataclass(frozen=True)
class Codec:
    """The pure functions a store needs to understand manifests and tombstones."""

    decode: Decode
    validate_query: ValidateQuery
    matches: Matches


DEFAULT_CODEC = Codec(objects.decode_object, objects.validate_query, objects.matches)


@ray.remote
class StoreActor:
    def __init__(self, name: str, config: StorageConfig, codec: Codec = DEFAULT_CODEC):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        self._core = StoreCore(
            name,
            config.data_dir / name,
            max_in_flight=config.store_concurrency,
            decode=codec.decode,
            validate_query=codec.validate_query,
            matches=codec.matches,
            cluster_params=config.cluster_params(),
        )
        self._stop = threading.Event()
        self._scrubber = threading.Thread(
            target=self._scrub_periodically,
            args=(config.scrub_interval_seconds,),
            name=f"scrub-{name}",
            daemon=True,
        )
        self._scrubber.start()
        logging.getLogger("dtn.store").info(
            "store=%s started pid=%d config=%s", name, os.getpid(), config.as_dict()
        )

    # Store interface (contract, section 7). Each call forwards to StoreCore.

    def put(self, obj_id: str, data: bytes, op_id: str | None = None) -> dict:
        return self._core.put(obj_id, data, op_id=op_id)

    def get(self, obj_id: str, op_id: str | None = None) -> bytes:
        return self._core.get(obj_id, op_id=op_id)

    def inventory(self, op_id: str | None = None) -> list[dict]:
        return self._core.inventory(op_id=op_id)

    def search(self, query: str, op_id: str | None = None) -> dict:
        return self._core.search(query, op_id=op_id)

    def tombstones(self, track_id: str, original: str, op_id: str | None = None) -> list:
        return self._core.tombstones(track_id, original, op_id=op_id)

    def delete_if_older(self, obj_id: str, seconds: float, op_id: str | None = None) -> dict:
        return self._core.delete_if_older(obj_id, seconds, op_id=op_id)

    def ping(self, op_id: str | None = None) -> dict:
        return self._core.ping(op_id=op_id)

    # Diagnostics, outside the store interface.

    def process_id(self) -> int:
        """PID of the process running this store now. Used to crash it on purpose."""
        return os.getpid()

    def _scrub_periodically(self, interval: float) -> None:
        log = logging.getLogger("dtn.store")
        while not self._stop.wait(interval):
            damaged = self._core.scrub()
            if damaged:
                log.warning("store=%s scrub quarantined %d objects", self._core.name, len(damaged))


def store_options(name: str, config: StorageConfig, namespace: str) -> dict:
    """Ray options for one store actor."""
    return {
        "name": name,
        "namespace": namespace,
        # Detached: stores outlive the process that started them, so every
        # `dtn` command can attach to the same running stores.
        "lifetime": "detached",
        "max_restarts": UNLIMITED_RESTARTS,
        "max_task_retries": 0,
        # Twice the store's own limit: the extra threads only run requests
        # that StoreCore rejects at once as saturated.
        "max_concurrency": 2 * config.store_concurrency,
        "num_cpus": 0,
    }
