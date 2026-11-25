"""Track operations: upload, search, list and withdraw (contract § 3.1, § 3.2, § 3.4).

Everything here runs in the caller's process and talks to storage only through
the ``Cluster`` interface, so the same code works with the in-memory
``FakeCluster`` and with the Ray cluster.

Each operation generates its operation id (principle P7), computes an absolute
deadline from its total timeout (decision «Cuánto puede durar una operación»)
and passes both to every cluster call.
"""

from __future__ import annotations  # `Client.list` shadows the builtin inside the class body

import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dtn import media
from dtn.cluster import Cluster, Scattered
from dtn.errors import Conflict, Invalid, NotFound, Unavailable
from dtn.ids import require_id, sha256_hex
from dtn.objects import (
    Manifest,
    Segment,
    Tombstone,
    decode_object,
    encode_manifest,
    encode_tombstone,
    make_manifest,
    normalize_text,
    validate_metadata,
    validate_query,
)

log = logging.getLogger("dtn.client")


@dataclass(frozen=True)
class ClientConfig:
    """Parameters of the track operations. ``config.py`` builds it from the
    ``DTN_*`` variables; the defaults are those of «Parámetros»."""

    upload_timeout_seconds: float = 600
    search_timeout_ms: float = 2000
    withdraw_timeout_seconds: float = 30
    upload_parallelism: int = 8
    segment_seconds: int = 2
    audio_bitrate: str = "128k"
    max_upload_seconds: float = 1200
    max_upload_bytes: int = 209_715_200

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ClientConfig":
        def read(name, kind, default):
            return kind(env[name]) if name in env else default

        d = cls()
        return cls(
            upload_timeout_seconds=read("DTN_UPLOAD_TIMEOUT_SECONDS", float, d.upload_timeout_seconds),
            search_timeout_ms=read("DTN_SEARCH_TIMEOUT_MS", float, d.search_timeout_ms),
            withdraw_timeout_seconds=read("DTN_WITHDRAW_TIMEOUT_SECONDS", float, d.withdraw_timeout_seconds),
            upload_parallelism=read("DTN_UPLOAD_PARALLELISM", int, d.upload_parallelism),
            segment_seconds=read("DTN_SEGMENT_SECONDS", int, d.segment_seconds),
            audio_bitrate=read("DTN_AUDIO_BITRATE", str, d.audio_bitrate),
            max_upload_seconds=read("DTN_MAX_UPLOAD_SECONDS", float, d.max_upload_seconds),
            max_upload_bytes=read("DTN_MAX_UPLOAD_BYTES", int, d.max_upload_bytes),
        )


@dataclass(frozen=True)
class TrackInfo:
    """One search result (contract § 3.2). ``duration`` is in seconds."""

    track_id: str
    title: str
    artist: str
    duration: float
    license: str


@dataclass(frozen=True)
class SearchResult:
    tracks: tuple[TrackInfo, ...]
    complete: bool
    unreachable: tuple[str, ...]


Encoder = Callable[[Path, ClientConfig, float], media.EncodedAudio]
"""Checks and re-encodes a file within a timeout in seconds; replaceable in tests."""


def ffmpeg_encoder(path: Path, config: ClientConfig, timeout: float) -> media.EncodedAudio:
    started = time.monotonic()
    media.check_file(path, media.MediaLimits(config.max_upload_seconds, config.max_upload_bytes), timeout=timeout)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise Unavailable("upload deadline passed while checking the file")
    return media.encode(path, media.EncodingSettings(config.segment_seconds, config.audio_bitrate), timeout=remaining)


def new_op_id() -> str:
    return uuid.uuid4().hex[:16]


class Client:
    """The client library for everything except ``play``."""

    def __init__(self, cluster: Cluster, config: ClientConfig | None = None, *, encoder: Encoder = ffmpeg_encoder):
        self.cluster = cluster
        self.config = config or ClientConfig()
        self.encoder = encoder
        if encoder is ffmpeg_encoder:
            # Another ffmpeg version may give other ids for the same upload (P6).
            try:
                log.info("encoder %s", media.ffmpeg_version())
            except Unavailable as exc:
                log.warning("encoder unavailable: %s", exc.message)

    # --- upload -----------------------------------------------------------

    def upload(self, path: str | os.PathLike, metadata: Mapping[str, object]) -> str:
        """Upload a track and return its id once it is confirmed (contract § 3.1)."""
        op_id = new_op_id()
        deadline = time.monotonic() + self.config.upload_timeout_seconds
        log.info("upload start op=%s file=%s", op_id, path)
        try:
            track_id = self._upload(Path(path), metadata, deadline, op_id)
        except Exception as exc:
            log.info("upload end op=%s error=%s", op_id, type(exc).__name__)
            raise
        log.info("upload end op=%s track=%s", op_id, track_id)
        return track_id

    def _upload(self, path: Path, raw_metadata: Mapping[str, object], deadline: float, op_id: str) -> str:
        # 1. Validate before doing anything expensive.
        try:
            metadata = validate_metadata(raw_metadata)
        except Invalid as exc:
            raise Invalid(exc.message, op_id=op_id) from None
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise Invalid(f"cannot read {path}: {exc.strerror}", op_id=op_id) from None
        if size > self.config.max_upload_bytes:
            raise Invalid(f"file is {size} bytes, more than the limit of {self.config.max_upload_bytes}", op_id=op_id)
        original = path.read_bytes()

        # 2. Re-encode. Same file and settings give the same bytes.
        try:
            encoded = self.encoder(path, self.config, max(deadline - time.monotonic(), 0))
        except (Invalid, Unavailable) as exc:
            raise type(exc)(exc.message, op_id=op_id) from None

        # 3. Build the manifest, and with it the id, before writing anything.
        objects: dict[str, bytes] = {sha256_hex(original): original, sha256_hex(encoded.init): encoded.init}
        segments = []
        for data, duration_ms in encoded.segments:
            seg_id = sha256_hex(data)
            objects[seg_id] = data  # identical segments (silence) are stored once
            segments.append(Segment(id=seg_id, duration_ms=duration_ms))
        manifest = make_manifest(
            metadata, init=sha256_hex(encoded.init), original=sha256_hex(original), segments=segments
        )
        manifest_bytes = encode_manifest(manifest)
        track_id = sha256_hex(manifest_bytes)

        # 4. No write at all if the track or its recording is withdrawn, or if
        #    that cannot be ruled out (decision «Qué pasa al volver a subir una
        #    pista retirada»).
        found, conclusive = self._find_tombstones(track_id, manifest.original, deadline, op_id)
        if found:
            raise Conflict(f"track {track_id} or its original recording was withdrawn: {found[0].reason}", op_id=op_id)
        if not conclusive:
            raise Unavailable("cannot rule out that the track was withdrawn: too many stores unreachable", op_id=op_id)

        # 5. Segments, init segment and original, in bounded parallel.
        with ThreadPoolExecutor(max_workers=self.config.upload_parallelism) as pool:
            futures = [
                pool.submit(self.cluster.put, obj_id, data, deadline=deadline, op_id=op_id)
                for obj_id, data in objects.items()
            ]
            for future in futures:
                future.result()  # re-raises the first failure: the outcome is then uncertain

        # 6. The manifest last: from now on the track is visible, and when it has
        #    N replicas, confirmed.
        self.cluster.put(track_id, manifest_bytes, deadline=deadline, op_id=op_id)
        return track_id

    # --- search / list ----------------------------------------------------

    def search(self, query: str) -> SearchResult:
        """Visible tracks whose title or artist contain every word of ``query`` (contract § 3.2)."""
        op_id = new_op_id()
        try:
            query = validate_query(query)
        except Invalid as exc:
            raise Invalid(exc.message, op_id=op_id) from None
        deadline = time.monotonic() + self.config.search_timeout_ms / 1000
        gathered = self.cluster.scatter("search", query, deadline=deadline, op_id=op_id)
        result = self._merge(gathered)
        log.info("search op=%s query=%r tracks=%d complete=%s unreachable=%s", op_id, query,
                 len(result.tracks), result.complete, ",".join(result.unreachable) or "-")
        return result

    def list(self) -> SearchResult:
        return self.search("")

    def _merge(self, gathered: Scattered) -> SearchResult:
        manifests: dict[str, Manifest] = {}
        withdrawn_tracks: set[str] = set()
        withdrawn_originals: set[str] = set()
        for answer in gathered.responses.values():
            for item in answer.get("manifests", ()):
                track_id, manifest = item["track_id"], item["manifest"]
                # A store already checks hashes; checking again costs little and
                # keeps a faulty store from naming a track after another.
                if isinstance(manifest, Manifest) and sha256_hex(encode_manifest(manifest)) == track_id:
                    manifests[track_id] = manifest
            for item in answer.get("tombstoned", ()):
                withdrawn_tracks.add(item["track_id"])
                withdrawn_originals.add(item["original"])
        tracks = [
            TrackInfo(track_id, m.title, m.artist, m.duration_ms / 1000, m.license)
            for track_id, m in manifests.items()
            if track_id not in withdrawn_tracks and m.original not in withdrawn_originals
        ]
        tracks.sort(key=lambda t: (t.artist, t.title, t.track_id))
        unreachable = tuple(sorted(gathered.unreachable))
        return SearchResult(tuple(tracks), len(unreachable) < self.cluster.replicas, unreachable)

    # --- withdraw ---------------------------------------------------------

    def withdraw(self, track_id: str, reason: str) -> None:
        """Withdraw a track and every track with the same original (contract § 3.4)."""
        op_id = new_op_id()
        require_id(track_id, op_id=op_id)
        if not isinstance(reason, str) or not normalize_text(reason):
            raise Invalid("reason must not be empty", op_id=op_id)
        reason = normalize_text(reason)
        deadline = time.monotonic() + self.config.withdraw_timeout_seconds
        log.info("withdraw start op=%s track=%s", op_id, track_id)

        manifest = self.fetch_manifest(track_id, deadline=deadline, op_id=op_id)
        found, _ = self._find_tombstones(track_id, manifest.original, deadline, op_id)
        if found:
            log.info("withdraw end op=%s already withdrawn", op_id)
            return
        tombstone = encode_tombstone(Tombstone(track_id=track_id, original=manifest.original, reason=reason))
        self.cluster.put_everywhere(sha256_hex(tombstone), tombstone, deadline=deadline, op_id=op_id)
        log.info("withdraw end op=%s ok", op_id)

    # --- shared with play -------------------------------------------------

    def fetch_manifest(self, track_id: str, *, deadline: float, op_id: str) -> Manifest:
        """Manifest of ``track_id``. ``NotFound`` only when its absence is certain
        (fewer than N stores unreachable), ``Unavailable`` otherwise; the cluster
        applies that rule (decision «Cuándo se responde «no existe» y cuándo «no
        disponible»»)."""
        data = self.cluster.get(track_id, deadline=deadline, op_id=op_id)
        manifest = decode_object(data)
        if not isinstance(manifest, Manifest):
            # The id names an object that is not a track, e.g. an audio segment.
            raise NotFound(f"{track_id} is not a track", op_id=op_id)
        return manifest

    def _find_tombstones(self, track_id: str, original: str, deadline: float, op_id: str) -> tuple[list[Tombstone], bool]:
        """Tombstones of the track or of its original, and whether "none" is conclusive.

        One valid tombstone from any store is enough. Its absence only counts
        when fewer than N stores were unreachable (decision «Cómo encuentra el
        cliente la lápida de una pista»).
        """
        gathered = self.cluster.scatter("tombstones", track_id, original, deadline=deadline, op_id=op_id)
        found = [
            t
            for answer in gathered.responses.values()
            for t in answer
            if isinstance(t, Tombstone) and (t.track_id == track_id or t.original == original)
        ]
        return found, len(gathered.unreachable) < self.cluster.replicas
