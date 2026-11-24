"""Manifests, tombstones, metadata validation and search matching.

Everything here is a pure function: no I/O, no Ray, no clocks. The stores use
``decode_object``, ``validate_query`` and ``matches``; the client library uses
the rest. Manifests and tombstones are written as canonical JSON (sorted keys,
no whitespace, UTF-8, integer milliseconds) so that the same content always
produces the same bytes, and therefore the same identifier.
"""

from collections.abc import Mapping
from dataclasses import dataclass

LICENSES = ("CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-NC-4.0", "public-domain")
MAX_TEXT_CHARS = 200
FORMAT_VERSION = 1


@dataclass(frozen=True)
class Metadata:
    """Fields supplied by whoever uploads a track."""

    title: str
    artist: str
    license: str


@dataclass(frozen=True)
class Segment:
    """One audio segment of a track: its object id and its length."""

    id: str
    duration_ms: int


@dataclass(frozen=True)
class Manifest:
    """A track. Its identifier is the SHA-256 of ``encode_manifest(manifest)``.

    ``duration_ms`` is the sum of the segment durations. ``init`` is the id of
    the initialization segment and ``original`` the id of the uploaded file.
    """

    title: str
    artist: str
    license: str
    duration_ms: int
    init: str
    original: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Tombstone:
    """Withdraws a recording: the track ``track_id`` and any track whose
    original file is ``original``."""

    track_id: str
    original: str
    reason: str


def validate_metadata(raw: Mapping[str, object]) -> Metadata:
    """Check user-supplied metadata and return it normalized.

    Raises ``Invalid`` unless ``title`` and ``artist`` are non-empty strings of
    at most ``MAX_TEXT_CHARS`` characters and ``license`` is one of
    ``LICENSES``. Any other field, ``duration`` included, is rejected.
    """
    raise NotImplementedError


def encode_manifest(manifest: Manifest) -> bytes:
    """Canonical bytes of a manifest, with ``"type": "manifest"`` and the format version."""
    raise NotImplementedError


def encode_tombstone(tombstone: Tombstone) -> bytes:
    """Canonical bytes of a tombstone, with ``"type": "tombstone"`` and the format version."""
    raise NotImplementedError


def decode_object(data: bytes) -> Manifest | Tombstone | None:
    """Recognize a stored object.

    Returns a ``Manifest`` or a ``Tombstone`` only if ``data`` is a well-formed
    one: known ``type`` and version, every field present with the right type,
    valid ids, ``duration_ms`` equal to the sum of the segments, and bytes
    identical to what the matching ``encode_*`` function produces. Anything
    else (audio segments, original files, malformed JSON) returns ``None`` and
    is stored as opaque bytes. Never raises.
    """
    raise NotImplementedError


def validate_query(query: str) -> str:
    """Return the query if it is a string of at most ``MAX_TEXT_CHARS`` characters;
    raise ``Invalid`` otherwise."""
    raise NotImplementedError


def matches(manifest: Manifest, query: str) -> bool:
    """True if every word of ``query`` appears inside ``title`` or ``artist``.

    Case-insensitive and accent-insensitive. The empty query matches every
    manifest. The result depends only on the arguments, so every store
    computes the same answer.
    """
    raise NotImplementedError
