"""In-memory ``Cluster`` with the semantics documented in ``dtn/cluster.py``.

It lets the track operations be built and tested without Ray and without the
real stores. Stores can be marked down, and a ``put`` can be made to fail after
writing to some stores, to reproduce a write interrupted halfway. Replicas are
placed with the same rendezvous order as the real cluster, so a test that
depends on placement behaves the same in both.

It also holds the in-memory equivalent of the store-side ``search`` and
``tombstones`` calls (contract § 7), which ``scatter`` dispatches to.
"""

import hashlib
import threading
import time
from collections.abc import Iterable
from typing import Any

from dtn.cluster import Scattered
from dtn.errors import Invalid, NotFound, Unavailable
from dtn.ids import require_id, sha256_hex
from dtn.objects import Manifest, Tombstone, decode_object, matches, validate_query


def placement_order(stores: Iterable[str], obj_id: str) -> list[str]:
    """Rendezvous hashing: stores sorted by SHA-256(name ‖ id), highest first."""
    return sorted(stores, key=lambda s: hashlib.sha256(f"{s}{obj_id}".encode()).digest(), reverse=True)


class FakeStore:
    """One store kept in memory: objects by id plus the manifest and tombstone indexes."""

    def __init__(self, name: str):
        self.name = name
        self.objects: dict[str, bytes] = {}

    def put(self, obj_id: str, data: bytes) -> str:
        existed = obj_id in self.objects
        self.objects[obj_id] = data
        return "existing" if existed else "new"

    def search(self, query: str) -> dict:
        validate_query(query)
        manifests, tombstoned = [], []
        for obj_id, data in self.objects.items():
            obj = decode_object(data)
            if isinstance(obj, Manifest) and matches(obj, query):
                manifests.append({"track_id": obj_id, "manifest": obj})
            elif isinstance(obj, Tombstone):
                tombstoned.append({"track_id": obj.track_id, "original": obj.original})
        return {"manifests": manifests, "tombstoned": tombstoned}

    def tombstones(self, track_id: str, original: str) -> list[Tombstone]:
        require_id(track_id)
        require_id(original)
        found = []
        for data in self.objects.values():
            obj = decode_object(data)
            if isinstance(obj, Tombstone) and (obj.track_id == track_id or obj.original == original):
                found.append(obj)
        return found


class FakeCluster:
    """``Cluster`` in memory. Thread-safe: uploads write in parallel."""

    def __init__(self, num_stores: int = 5, replicas: int = 3):
        self.stores = tuple(f"store-{i}" for i in range(1, num_stores + 1))
        self.replicas = replicas
        self._store = {name: FakeStore(name) for name in self.stores}
        self._down: set[str] = set()
        self._fail_after: dict[str, int] = {}
        self._lock = threading.Lock()
        self.puts: list[tuple[str, str]] = []
        """Every (store, id) written, in order, for tests to inspect."""

    # --- fault injection --------------------------------------------------

    def down(self, *names: str) -> None:
        self._down.update(names)

    def up(self, *names: str) -> None:
        self._down.difference_update(names)

    def fail_put_after(self, obj_id: str, writes: int) -> None:
        """The next ``put`` of ``obj_id`` writes to ``writes`` stores and then raises ``Unavailable``."""
        self._fail_after[obj_id] = writes

    # --- inspection -------------------------------------------------------

    def holders(self, obj_id: str) -> list[str]:
        return [name for name in self.stores if obj_id in self._store[name].objects]

    def all_ids(self) -> set[str]:
        return {obj_id for store in self._store.values() for obj_id in store.objects}

    def store(self, name: str) -> FakeStore:
        return self._store[name]

    # --- Cluster ----------------------------------------------------------

    def _alive(self) -> list[str]:
        return [name for name in self.stores if name not in self._down]

    @staticmethod
    def _expired(deadline: float) -> bool:
        return time.monotonic() >= deadline

    def put(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        if sha256_hex(data) != obj_id:
            raise Invalid(f"data does not hash to {obj_id}", op_id=op_id)
        if self._expired(deadline):
            raise Unavailable("deadline passed before writing", op_id=op_id)
        with self._lock:
            budget = self._fail_after.pop(obj_id, None)
            written = 0
            for name in placement_order(self._alive(), obj_id):
                if written == self.replicas:
                    break
                if budget is not None and written == budget:
                    raise Unavailable(f"write of {obj_id} interrupted after {written} replicas", op_id=op_id)
                self._store[name].put(obj_id, data)
                self.puts.append((name, obj_id))
                written += 1
            if written < self.replicas:
                raise Unavailable(f"only {written} of {self.replicas} replicas confirmed", op_id=op_id)

    def put_everywhere(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        if sha256_hex(data) != obj_id:
            raise Invalid(f"data does not hash to {obj_id}", op_id=op_id)
        if self._expired(deadline):
            raise Unavailable("deadline passed before writing", op_id=op_id)
        with self._lock:
            alive = self._alive()
            for name in alive:
                self._store[name].put(obj_id, data)
                self.puts.append((name, obj_id))
            if len(alive) < self.replicas:
                raise Unavailable(f"only {len(alive)} of {self.replicas} replicas confirmed", op_id=op_id)

    def get(self, obj_id: str, *, deadline: float, op_id: str) -> bytes:
        if self._expired(deadline):
            raise Unavailable("deadline passed before reading", op_id=op_id)
        with self._lock:
            alive = self._alive()
            for name in placement_order(alive, obj_id):
                data = self._store[name].objects.get(obj_id)
                if data is not None and sha256_hex(data) == obj_id:
                    return data
        unreachable = len(self.stores) - len(alive)
        if unreachable < self.replicas:
            raise NotFound(f"no store has {obj_id}", op_id=op_id)
        raise Unavailable(f"{obj_id} not found and {unreachable} stores unreachable", op_id=op_id)

    def scatter(self, method: str, *args: Any, deadline: float, op_id: str) -> Scattered:
        if method not in ("search", "tombstones"):
            raise ValueError(f"unknown store method {method!r}")
        if self._expired(deadline):
            return Scattered(responses={}, unreachable=self.stores)
        responses, unreachable = {}, []
        with self._lock:  # uploads write from other threads
            for name in self.stores:
                if name in self._down:
                    unreachable.append(name)
                else:
                    responses[name] = getattr(self._store[name], method)(*args)
        return Scattered(responses=responses, unreachable=tuple(unreachable))
