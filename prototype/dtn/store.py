"""One store: durable, self-checking storage of immutable objects.

``StoreCore`` holds all the logic of a store and knows nothing about Ray, so
it can be tested directly and translated to the native version as is. The
Ray actor in ``store_actor`` only wraps it.

Layout of a store directory::

    objects/<id>      one file per object, named by its SHA-256
    quarantine/<id>   copies whose bytes no longer match their name
    tmp/              half-written files; emptied at startup

Thread safety: several calls run at once (the actor is threaded). One lock
guards the in-memory index and the in-flight counter. It is held only for
short in-memory steps, plus the unlink in ``delete_if_older`` so that a
rewrite cannot slip between the age check and the deletion. File reads and
writes happen outside it.
"""

import logging
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dtn.errors import Corrupt, Invalid, NotFound, Unavailable
from dtn.ids import require_id, sha256_hex
from dtn.objects import Manifest, Tombstone

log = logging.getLogger("dtn.store")

Decode = Callable[[bytes], Manifest | Tombstone | None]
ValidateQuery = Callable[[str], str]
Matches = Callable[[Manifest, str], bool]


@dataclass
class _Entry:
    size: int
    written: float  # monotonic time of the last write, or of startup


class StoreCore:
    def __init__(
        self,
        name: str,
        directory: Path,
        *,
        max_in_flight: int,
        decode: Decode,
        validate_query: ValidateQuery,
        matches: Matches,
        cluster_params: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """``cluster_params`` (S and N) are only reported by ``ping``, so that
        clients can check they use the same ones."""
        self.name = name
        self._cluster_params = dict(cluster_params or {})
        self.incarnation = uuid.uuid4().hex
        self._max_in_flight = max_in_flight
        self._decode = decode
        self._validate_query = validate_query
        self._matches = matches
        self._clock = clock

        self._objects_dir = Path(directory) / "objects"
        self._quarantine_dir = Path(directory) / "quarantine"
        self._tmp_dir = Path(directory) / "tmp"
        for d in (self._objects_dir, self._quarantine_dir, self._tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._in_flight = 0
        self._entries: dict[str, _Entry] = {}
        self._manifests: dict[str, Manifest] = {}
        self._tombstones: dict[str, Tombstone] = {}
        self._tombstones_by_key: dict[str, set[str]] = {}  # track_id or original -> ids

        self._recover()

    # ------------------------------------------------------------------
    # Startup

    def _recover(self) -> None:
        """Rebuild the index from disk. Every object starts with age 0."""
        for leftover in self._tmp_dir.iterdir():
            leftover.unlink(missing_ok=True)

        started = self._clock()
        for path in self._objects_dir.iterdir():
            obj_id = path.name
            try:
                require_id(obj_id)
            except Invalid:
                log.warning("store=%s ignoring stray file %s", self.name, path)
                continue
            size = path.stat().st_size
            # Manifests and tombstones are JSON, so they start with "{".
            # Audio segments and original files never do.
            with path.open("rb") as f:
                looks_indexed = f.read(1) == b"{"
            if looks_indexed:
                data = path.read_bytes()
                if sha256_hex(data) != obj_id:
                    self._quarantine_file(obj_id)
                    continue
                self._index(obj_id, self._decode(data))
            self._entries[obj_id] = _Entry(size=size, written=started)

        log.info(
            "store=%s incarnation=%s recovered objects=%d manifests=%d tombstones=%d",
            self.name,
            self.incarnation,
            len(self._entries),
            len(self._manifests),
            len(self._tombstones),
        )

    # ------------------------------------------------------------------
    # Store interface

    def put(self, obj_id: str, data: bytes, *, op_id: str | None = None) -> dict:
        with self._request("put", obj_id, op_id):
            require_id(obj_id, op_id=op_id)
            if sha256_hex(data) != obj_id:
                raise Invalid("data does not match its id", op_id=op_id)

            if self._renew_if_valid(obj_id):
                return {"stored": "existing"}

            try:
                self._write_atomically(obj_id, data)
            except OSError as e:
                raise Unavailable(f"disk error: {e}", op_id=op_id) from e

            decoded = self._decode(data)  # outside the lock: parsing may be slow
            with self._lock:
                self._entries[obj_id] = _Entry(size=len(data), written=self._clock())
                self._index(obj_id, decoded)
            return {"stored": "new"}

    def get(self, obj_id: str, *, op_id: str | None = None) -> bytes:
        with self._request("get", obj_id, op_id):
            require_id(obj_id, op_id=op_id)
            with self._lock:
                known = obj_id in self._entries
            if not known:
                raise NotFound(f"{self.name} does not have {obj_id}", op_id=op_id)
            try:
                data = (self._objects_dir / obj_id).read_bytes()
            except FileNotFoundError:
                raise NotFound(f"{self.name} does not have {obj_id}", op_id=op_id) from None
            except OSError as e:
                raise Unavailable(f"disk error: {e}", op_id=op_id) from e
            if sha256_hex(data) != obj_id:
                self._quarantine(obj_id)
                raise Corrupt(f"{self.name} holds a damaged copy of {obj_id}", op_id=op_id)
            return data

    def inventory(self, *, op_id: str | None = None) -> list[dict]:
        with self._request("inventory", None, op_id):
            now = self._clock()
            with self._lock:
                return [
                    {
                        "id": obj_id,
                        "bytes": e.size,
                        "age_seconds": now - e.written,
                        "kind": self._kind(obj_id),
                    }
                    for obj_id, e in self._entries.items()
                ]

    def search(self, query: str, *, op_id: str | None = None) -> dict:
        with self._request("search", None, op_id):
            self._validate_query(query)
            with self._lock:
                manifests = list(self._manifests.items())
                tombstones = list(self._tombstones.values())
            return {
                "manifests": [
                    {"track_id": track_id, "manifest": m}
                    for track_id, m in manifests
                    if self._matches(m, query)
                ],
                "tombstoned": [
                    {"track_id": t.track_id, "original": t.original} for t in tombstones
                ],
            }

    def tombstones(self, track_id: str, original: str, *, op_id: str | None = None) -> list:
        with self._request("tombstones", track_id, op_id):
            require_id(track_id, op_id=op_id)
            require_id(original, op_id=op_id)
            with self._lock:
                ids = self._tombstones_by_key.get(track_id, set()) | self._tombstones_by_key.get(
                    original, set()
                )
                return [self._tombstones[i] for i in sorted(ids)]

    def delete_if_older(self, obj_id: str, seconds: float, *, op_id: str | None = None) -> dict:
        with self._request("delete_if_older", obj_id, op_id):
            require_id(obj_id, op_id=op_id)
            with self._lock:
                entry = self._entries.get(obj_id)
                if entry is None:
                    return {"deleted": False, "reason": "absent"}
                if self._clock() - entry.written <= seconds:
                    return {"deleted": False, "reason": "recent"}
                # Unlinking under the lock keeps a concurrent put of the same
                # object from renewing it between the check and the deletion.
                # No fsync: if the deletion is lost in a crash, the object
                # comes back and is collected again later.
                (self._objects_dir / obj_id).unlink(missing_ok=True)
                self._forget(obj_id)
            return {"deleted": True, "reason": "deleted"}

    def ping(self, *, op_id: str | None = None) -> dict:
        with self._request("ping", None, op_id):
            with self._lock:
                count = len(self._entries)
            return {
                "store": self.name,
                "incarnation": self.incarnation,
                "objects": count,
                **self._cluster_params,
            }

    # ------------------------------------------------------------------
    # Background integrity check

    def scrub(self) -> list[str]:
        """Re-hash every object and quarantine the damaged ones.

        Runs in the background, outside the in-flight limit. Returns the ids
        it quarantined so the caller can log them; repair restores them.
        """
        with self._lock:
            ids = list(self._entries)
        damaged = []
        for obj_id in ids:
            try:
                data = (self._objects_dir / obj_id).read_bytes()
            except FileNotFoundError:
                continue  # deleted meanwhile
            if sha256_hex(data) != obj_id:
                self._quarantine(obj_id)
                damaged.append(obj_id)
        return damaged

    # ------------------------------------------------------------------
    # Internals

    @contextmanager
    def _request(self, call: str, obj_id: str | None, op_id: str | None):
        """Admission control and logging (principle P7) around every call."""
        with self._lock:
            if self._in_flight >= self._max_in_flight:
                log.info("store=%s op=%s call=%s rejected=saturated", self.name, op_id, call)
                raise Unavailable(f"{self.name} is saturated", saturated=True, op_id=op_id)
            self._in_flight += 1
        log.debug("store=%s op=%s call=%s id=%s start", self.name, op_id, call, obj_id)
        outcome = "ok"
        try:
            yield
        except Exception as e:
            outcome = type(e).__name__
            raise
        finally:
            with self._lock:
                self._in_flight -= 1
            log.info("store=%s op=%s call=%s id=%s result=%s", self.name, op_id, call, obj_id, outcome)

    def _renew_if_valid(self, obj_id: str) -> bool:
        """If a valid copy exists, renew its age and return True.

        The copy is re-hashed first: confirming a damaged copy as a replica
        would leave the object with fewer valid replicas than reported.
        """
        with self._lock:
            if obj_id not in self._entries:
                return False
        try:
            data = (self._objects_dir / obj_id).read_bytes()
        except FileNotFoundError:
            return False
        if sha256_hex(data) != obj_id:
            self._quarantine(obj_id)
            return False
        with self._lock:
            entry = self._entries.get(obj_id)
            if entry is None:  # deleted after we read it: write it again
                return False
            entry.written = self._clock()
            return True

    def _write_atomically(self, obj_id: str, data: bytes) -> None:
        """Write to a temporary file, force it to disk, then rename it into place.

        A crash at any point leaves either no file or the complete one, never
        a partial file under the object's name.
        """
        fd, tmp_path = tempfile.mkstemp(dir=self._tmp_dir, prefix=obj_id[:16] + ".")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._objects_dir / obj_id)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        _fsync_directory(self._objects_dir)

    def _index(self, obj_id: str, obj: Manifest | Tombstone | None) -> None:
        """Index manifests and tombstones. Caller holds the lock (or is in startup)."""
        if isinstance(obj, Manifest):
            self._manifests[obj_id] = obj
        elif isinstance(obj, Tombstone):
            self._tombstones[obj_id] = obj
            for key in (obj.track_id, obj.original):
                self._tombstones_by_key.setdefault(key, set()).add(obj_id)

    def _kind(self, obj_id: str) -> str:
        """"manifest", "tombstone" or "data" (audio and originals). Caller holds the lock."""
        if obj_id in self._manifests:
            return "manifest"
        if obj_id in self._tombstones:
            return "tombstone"
        return "data"

    def _forget(self, obj_id: str) -> None:
        """Drop an object from every index. Caller holds the lock."""
        self._entries.pop(obj_id, None)
        self._manifests.pop(obj_id, None)
        tombstone = self._tombstones.pop(obj_id, None)
        if tombstone is not None:
            for key in (tombstone.track_id, tombstone.original):
                ids = self._tombstones_by_key.get(key)
                if ids is not None:
                    ids.discard(obj_id)
                    if not ids:
                        del self._tombstones_by_key[key]

    def _quarantine(self, obj_id: str) -> None:
        with self._lock:
            self._forget(obj_id)
            self._quarantine_file(obj_id)

    def _quarantine_file(self, obj_id: str) -> None:
        try:
            os.replace(self._objects_dir / obj_id, self._quarantine_dir / obj_id)
        except FileNotFoundError:
            return
        log.warning("store=%s quarantined damaged copy of %s", self.name, obj_id)


def _fsync_directory(path: Path) -> None:
    """Make a rename in ``path`` durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
