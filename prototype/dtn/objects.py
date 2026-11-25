"""Manifests, tombstones, metadata validation and search matching.

Everything here is a pure function: no I/O, no Ray, no clocks. The stores use
``decode_object``, ``validate_query`` and ``matches``; the client library uses
the rest. Manifests and tombstones are written as canonical JSON (sorted keys,
no whitespace, UTF-8, integer milliseconds) so that the same content always
produces the same bytes, and therefore the same identifier.
"""

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from dtn.errors import Invalid
from dtn.ids import is_valid_id

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


def normalize_text(value: str) -> str:
    """NFC form without surrounding whitespace.

    The same title typed on two systems can reach us as different code points
    (an accented letter precomposed or as letter + combining accent). Without
    this, both would give different manifests and therefore two tracks.
    """
    return unicodedata.normalize("NFC", value).strip()


def require_utf8(value: str, name: str) -> str:
    """Raise ``Invalid`` if ``value`` cannot be written as UTF-8.

    Python strings can hold lone surrogates, for example when a command-line
    argument has bytes that are not UTF-8. They cannot go into a manifest.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{name} is not valid text") from None
    return value


def validate_metadata(raw: Mapping[str, object]) -> Metadata:
    """Check user-supplied metadata and return it normalized.

    Raises ``Invalid`` unless ``title`` and ``artist`` are non-empty strings of
    at most ``MAX_TEXT_CHARS`` characters and ``license`` is one of
    ``LICENSES``. Any other field, ``duration`` included, is rejected.
    ``title`` and ``artist`` are returned in NFC form without surrounding
    whitespace, and the limits apply after normalizing.
    """
    if not isinstance(raw, Mapping):
        raise Invalid("metadata must be a mapping")
    unknown = sorted(map(repr, set(raw) - {"title", "artist", "license"}))
    if unknown:
        raise Invalid(f"unknown metadata fields: {', '.join(unknown)}")
    texts = {}
    for name in ("title", "artist"):
        value = raw.get(name)
        if not isinstance(value, str):
            raise Invalid(f"{name} must be a string")
        value = normalize_text(value)
        require_utf8(value, name)
        if not value:
            raise Invalid(f"{name} must not be empty")
        if len(value) > MAX_TEXT_CHARS:
            raise Invalid(f"{name} is longer than {MAX_TEXT_CHARS} characters")
        texts[name] = value
    license_ = raw.get("license")
    if license_ not in LICENSES:
        raise Invalid(f"license must be one of: {', '.join(LICENSES)}")
    return Metadata(title=texts["title"], artist=texts["artist"], license=license_)


def make_manifest(metadata: Metadata, *, init: str, original: str, segments) -> Manifest:
    """Build the manifest of a track; ``duration_ms`` is the sum of the segments."""
    segments = tuple(segments)
    return Manifest(
        title=metadata.title,
        artist=metadata.artist,
        license=metadata.license,
        duration_ms=sum(s.duration_ms for s in segments),
        init=init,
        original=original,
        segments=segments,
    )


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode_manifest(manifest: Manifest) -> bytes:
    """Canonical bytes of a manifest, with ``"type": "manifest"`` and the format version."""
    return _canonical(
        {
            "artist": manifest.artist,
            "duration_ms": manifest.duration_ms,
            "init": manifest.init,
            "license": manifest.license,
            "original": manifest.original,
            "segments": [{"duration_ms": s.duration_ms, "id": s.id} for s in manifest.segments],
            "title": manifest.title,
            "type": "manifest",
            "version": FORMAT_VERSION,
        }
    )


def encode_tombstone(tombstone: Tombstone) -> bytes:
    """Canonical bytes of a tombstone, with ``"type": "tombstone"`` and the format version."""
    return _canonical(
        {
            "original": tombstone.original,
            "reason": tombstone.reason,
            "track_id": tombstone.track_id,
            "type": "tombstone",
            "version": FORMAT_VERSION,
        }
    )


_MANIFEST_KEYS = {"artist", "duration_ms", "init", "license", "original", "segments", "title", "type", "version"}
_TOMBSTONE_KEYS = {"original", "reason", "track_id", "type", "version"}


def _is_count(value: object) -> bool:
    # bool is a subclass of int in Python; a duration of `true` is not a duration.
    return type(value) is int and value >= 0


def _is_text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT_CHARS and normalize_text(value) == value


def _decode_manifest(doc: dict) -> Manifest | None:
    if set(doc) != _MANIFEST_KEYS:
        return None
    if not (_is_text(doc["title"]) and _is_text(doc["artist"]) and doc["license"] in LICENSES):
        return None
    if not (is_valid_id(doc["init"]) and is_valid_id(doc["original"]) and _is_count(doc["duration_ms"])):
        return None
    raw_segments = doc["segments"]
    if not isinstance(raw_segments, list) or not raw_segments:
        return None
    segments = []
    for item in raw_segments:
        if not isinstance(item, dict) or set(item) != {"duration_ms", "id"}:
            return None
        if not (is_valid_id(item["id"]) and _is_count(item["duration_ms"])):
            return None
        segments.append(Segment(id=item["id"], duration_ms=item["duration_ms"]))
    if sum(s.duration_ms for s in segments) != doc["duration_ms"]:
        return None
    return Manifest(
        title=doc["title"],
        artist=doc["artist"],
        license=doc["license"],
        duration_ms=doc["duration_ms"],
        init=doc["init"],
        original=doc["original"],
        segments=tuple(segments),
    )


def _decode_tombstone(doc: dict) -> Tombstone | None:
    if set(doc) != _TOMBSTONE_KEYS:
        return None
    if not (is_valid_id(doc["track_id"]) and is_valid_id(doc["original"])):
        return None
    if not _is_text(doc["reason"]):
        return None
    reason = doc["reason"]
    return Tombstone(track_id=doc["track_id"], original=doc["original"], reason=reason)


def decode_object(data: bytes) -> Manifest | Tombstone | None:
    """Recognize a stored object.

    Returns a ``Manifest`` or a ``Tombstone`` only if ``data`` is a well-formed
    one: known ``type`` and version, every field present with the right type,
    valid ids, ``duration_ms`` equal to the sum of the segments, and bytes
    identical to what the matching ``encode_*`` function produces. Anything
    else (audio segments, original files, malformed JSON) returns ``None`` and
    is stored as opaque bytes. Never raises.
    """
    try:
        # Cheap rejection for audio: a JSON object starts with "{".
        if not data or data[:1] != b"{":
            return None
        doc = json.loads(data.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("version") != FORMAT_VERSION or type(doc.get("version")) is not int:
            return None
        if doc.get("type") == "manifest":
            obj = _decode_manifest(doc)
            encoded = encode_manifest(obj) if obj is not None else None
        elif doc.get("type") == "tombstone":
            obj = _decode_tombstone(doc)
            encoded = encode_tombstone(obj) if obj is not None else None
        else:
            return None
        # Only the canonical spelling counts: otherwise one content could have
        # several identifiers.
        return obj if encoded == data else None
    except Exception:  # noqa: BLE001 - must never raise, whatever the bytes are
        return None


def validate_query(query: str) -> str:
    """Return the query if it is a string of at most ``MAX_TEXT_CHARS`` characters;
    raise ``Invalid`` otherwise."""
    if not isinstance(query, str):
        raise Invalid("query must be a string")
    if len(query) > MAX_TEXT_CHARS:
        raise Invalid(f"query is longer than {MAX_TEXT_CHARS} characters")
    return query


def _fold(text: str) -> str:
    """Lowercase and without accents: "Canción" and "CANCION" fold the same."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def matches(manifest: Manifest, query: str) -> bool:
    """True if every word of ``query`` appears inside ``title`` or ``artist``.

    Case-insensitive and accent-insensitive. The empty query matches every
    manifest. The result depends only on the arguments, so every store
    computes the same answer.
    """
    title = _fold(manifest.title)
    artist = _fold(manifest.artist)
    return all(word in title or word in artist for word in _fold(query).split())
