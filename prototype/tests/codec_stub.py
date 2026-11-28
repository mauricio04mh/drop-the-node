"""Stand-in for the manifest format of ``dtn.objects`` while it is written.

The store only needs to tell manifests and tombstones apart from audio and to
filter manifests by a query, so a tiny JSON format is enough to test it.
"""

import json
import time

from dtn.errors import Invalid
from dtn.objects import Manifest, Segment, Tombstone

OTHER_ID = "0" * 64
SLOW_MARKER = b"slow-to-index:"


def encode(obj) -> bytes:
    if isinstance(obj, Manifest):
        body = {
            "type": "manifest",
            "title": obj.title,
            "artist": obj.artist,
            "init": obj.init,
            "original": obj.original,
            "segments": [s.id for s in obj.segments],
        }
    else:
        body = {"type": "tombstone", "track_id": obj.track_id, "original": obj.original}
    return json.dumps(body, sort_keys=True).encode()


def decode(data: bytes):
    if data.startswith(SLOW_MARKER):
        # Lets a test keep a request in flight for a while.
        time.sleep(float(data[len(SLOW_MARKER) :]))
        return None
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if body.get("type") == "manifest":
        return make_manifest(
            body["title"],
            body["artist"],
            init=body.get("init", OTHER_ID),
            original=body.get("original", OTHER_ID),
            segments=body.get("segments", (OTHER_ID,)),
        )
    if body.get("type") == "tombstone":
        return Tombstone(body["track_id"], body["original"], "")
    return None


def validate_query(query: str) -> str:
    if len(query) > 200:
        raise Invalid("query too long")
    return query


def matches(manifest: Manifest, query: str) -> bool:
    return query.lower() in f"{manifest.title} {manifest.artist}".lower()


def make_manifest(
    title: str,
    artist: str,
    *,
    init: str = OTHER_ID,
    original: str = OTHER_ID,
    segments=(OTHER_ID,),
) -> Manifest:
    """A manifest; the ids it references default to ``OTHER_ID``."""
    parts = tuple(Segment(s, 2000) for s in segments)
    return Manifest(title, artist, "CC0-1.0", 2000 * len(parts), init, original, parts)
