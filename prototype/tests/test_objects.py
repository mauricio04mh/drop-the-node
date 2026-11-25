"""Manifests, tombstones, metadata validation and search matching."""

import json
import unicodedata

import pytest

from dtn.errors import Invalid
from dtn.ids import sha256_hex
from dtn.objects import (
    LICENSES,
    MAX_TEXT_CHARS,
    Manifest,
    Metadata,
    Segment,
    Tombstone,
    decode_object,
    encode_manifest,
    encode_tombstone,
    make_manifest,
    matches,
    validate_metadata,
    validate_query,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64


def manifest(title="Canción del mar", artist="Ana", license="CC-BY-4.0") -> Manifest:
    return make_manifest(
        Metadata(title=title, artist=artist, license=license),
        init=A,
        original=B,
        segments=[Segment(id=C, duration_ms=2000), Segment(id=D, duration_ms=1500)],
    )


# --- validate_metadata ----------------------------------------------------


def test_valid_metadata_is_returned():
    md = validate_metadata({"title": "Hola", "artist": "Ana", "license": "CC0-1.0"})
    assert md == Metadata(title="Hola", artist="Ana", license="CC0-1.0")


def test_metadata_is_normalized_to_nfc_without_surrounding_spaces():
    decomposed = unicodedata.normalize("NFD", "  Canción ")
    md = validate_metadata({"title": decomposed, "artist": "Ana", "license": "CC0-1.0"})
    assert md.title == unicodedata.normalize("NFC", "Canción")


def test_same_title_typed_two_ways_gives_the_same_track():
    nfc = validate_metadata({"title": "Café", "artist": "Ana", "license": "CC0-1.0"})
    nfd = validate_metadata({"title": unicodedata.normalize("NFD", "Café"), "artist": "Ana", "license": "CC0-1.0"})
    m1 = make_manifest(nfc, init=A, original=B, segments=[Segment(C, 1)])
    m2 = make_manifest(nfd, init=A, original=B, segments=[Segment(C, 1)])
    assert sha256_hex(encode_manifest(m1)) == sha256_hex(encode_manifest(m2))


@pytest.mark.parametrize(
    "raw",
    [
        {"title": "", "artist": "Ana", "license": "CC0-1.0"},
        {"title": "   ", "artist": "Ana", "license": "CC0-1.0"},
        {"title": "Hola", "artist": "", "license": "CC0-1.0"},
        {"title": "x" * (MAX_TEXT_CHARS + 1), "artist": "Ana", "license": "CC0-1.0"},
        {"title": "Hola", "artist": "Ana", "license": "MIT"},
        {"title": "Hola", "artist": "Ana"},
        {"title": 3, "artist": "Ana", "license": "CC0-1.0"},
        {"title": "Hola", "artist": "Ana", "license": "CC0-1.0", "duration": 120},
        {"title": "Hola", "artist": "Ana", "license": "CC0-1.0", "genre": "pop"},
    ],
)
def test_invalid_metadata_is_rejected(raw):
    with pytest.raises(Invalid):
        validate_metadata(raw)


def test_title_at_the_limit_is_accepted():
    md = validate_metadata({"title": "x" * MAX_TEXT_CHARS, "artist": "Ana", "license": LICENSES[0]})
    assert len(md.title) == MAX_TEXT_CHARS


# --- encoding ---------------------------------------------------------------


def test_manifest_encoding_is_canonical_json():
    data = encode_manifest(manifest())
    assert data == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert b" " not in data.replace("Canción del mar".encode(), b"")
    assert "Canción".encode("utf-8") in data  # UTF-8, not \u escapes
    doc = json.loads(data)
    assert doc["type"] == "manifest" and doc["version"] == 1
    assert doc["duration_ms"] == 3500


def test_encoding_is_deterministic():
    assert encode_manifest(manifest()) == encode_manifest(manifest())
    t = Tombstone(track_id=A, original=B, reason="sin licencia")
    assert encode_tombstone(t) == encode_tombstone(Tombstone(A, B, "sin licencia"))


def test_manifest_round_trip():
    m = manifest()
    assert decode_object(encode_manifest(m)) == m


def test_tombstone_round_trip():
    t = Tombstone(track_id=A, original=B, reason="sin licencia")
    data = encode_tombstone(t)
    assert json.loads(data) == {"original": B, "reason": "sin licencia", "track_id": A, "type": "tombstone", "version": 1}
    assert decode_object(data) == t


# --- decode_object rejects everything else --------------------------------


def _manifest_doc() -> dict:
    return json.loads(encode_manifest(manifest()))


def _canon(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(type="playlist"),
        lambda d: d.update(version=2),
        lambda d: d.update(version=True),
        lambda d: d.update(version=1.0),
        lambda d: d.pop("init"),
        lambda d: d.update(extra=1),
        lambda d: d.update(duration_ms=3499),
        lambda d: d.update(duration_ms=3500.0),
        lambda d: d.update(duration_ms=True),
        lambda d: d.update(init="ABC"),
        lambda d: d.update(license="MIT"),
        lambda d: d.update(title=""),
        lambda d: d.update(title=" Hola"),
        lambda d: d.update(segments=[]),
        lambda d: d["segments"][0].update(id="nothex"),
        lambda d: d["segments"][0].update(extra=0),
    ],
)
def test_malformed_manifests_are_not_recognized(mutate):
    doc = _manifest_doc()
    mutate(doc)
    assert decode_object(_canon(doc)) is None


def test_non_canonical_spelling_is_not_recognized():
    doc = _manifest_doc()
    pretty = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False).encode()
    escaped = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    assert decode_object(pretty) is None
    assert decode_object(escaped) is None


def test_duplicate_keys_are_not_recognized():
    data = encode_manifest(manifest())
    assert decode_object(data[:-1] + b',"version":1}') is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\x00\x00\x18ftypiso6",  # start of an fMP4 segment
        b"ID3\x04\x00",  # start of an MP3
        b"{",
        b"{}",
        b"[1,2]",
        b"{\xff\xfe}",
        b'{"type":"tombstone","version":1}',
        "{\"reason\":\"x\",\"track_id\":\"" .encode() + A.encode() + b'"}',
    ],
)
def test_other_bytes_are_opaque_and_never_raise(data):
    assert decode_object(data) is None


def test_tombstone_with_empty_reason_is_not_recognized():
    assert decode_object(_canon({"original": B, "reason": "", "track_id": A, "type": "tombstone", "version": 1})) is None


# --- queries --------------------------------------------------------------


def test_query_length_limit():
    assert validate_query("x" * MAX_TEXT_CHARS) == "x" * MAX_TEXT_CHARS
    with pytest.raises(Invalid):
        validate_query("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(Invalid):
        validate_query(None)


@pytest.mark.parametrize(
    "query",
    ["", "   ", "mar", "MAR", "cancion", "CANCIÓN", "ana mar", "mar ana", "del", "an"],
)
def test_matching_queries(query):
    assert matches(manifest(), query)


@pytest.mark.parametrize("query", ["sol", "mar luna", "ana-mar", "canciones"])
def test_non_matching_queries(query):
    assert not matches(manifest(), query)


def test_a_word_must_fit_inside_one_field():
    # "ana" is in the artist and "del" in the title, but "anadel" is in neither.
    assert not matches(manifest(), "anadel")


def test_text_that_is_not_utf8_is_invalid():
    # A command-line argument with bytes that are not UTF-8 arrives as a lone surrogate.
    with pytest.raises(Invalid):
        validate_metadata({"title": "caf\udce9", "artist": "Ana", "license": "CC0-1.0"})


def test_unknown_fields_of_any_type_are_reported():
    with pytest.raises(Invalid, match="unknown metadata fields"):
        validate_metadata({1: "x", "x": 2, "title": "a", "artist": "b", "license": "CC0-1.0"})
