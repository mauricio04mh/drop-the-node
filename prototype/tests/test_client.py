"""Track operations against the in-memory cluster: the acceptance criteria of issue #6."""

import json
import shutil
import subprocess

import pytest

from dtn.client import Client
from dtn.config import ClientConfig, load_client_config
from dtn.errors import Conflict, Invalid, NotFound, Unavailable
from dtn.fake_cluster import FakeCluster
from dtn.ids import sha256_hex
from dtn.media import EncodedAudio
from dtn.objects import Manifest, Tombstone, decode_object

LICENSE = "CC-BY-4.0"


def fake_encoder(path, config, timeout):
    """Deterministic stand-in for ffmpeg: splits the file into 3 'segments'."""
    data = path.read_bytes()
    third = max(-(-len(data) // 3), 1)  # ceiling: exactly 3 chunks
    chunks = [data[i : i + third] for i in range(0, len(data), third)]
    return EncodedAudio(init=b"init:" + data[:8], segments=tuple((b"seg:" + c, 2000) for c in chunks))


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def client(cluster):
    return Client(cluster, ClientConfig(), encoder=fake_encoder)


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "song.wav"
    path.write_bytes(b"RIFF fake audio content for the tests " * 10)
    return path


@pytest.fixture
def other_song(tmp_path):
    path = tmp_path / "other.wav"
    path.write_bytes(b"RIFF a different recording " * 10)
    return path


def md(title="Canción del mar", artist="Ana"):
    return {"title": title, "artist": artist, "license": LICENSE}


def ids(result):
    return [t.track_id for t in result.tracks]


# --- with 0, 1 and 2 stores down, everything works ------------------------


@pytest.mark.parametrize("down", [0, 1, 2])
def test_upload_search_withdraw_with_up_to_two_stores_down(cluster, client, song, down):
    cluster.down(*cluster.stores[:down])
    track_id = client.upload(song, md())
    result = client.search("mar")
    assert ids(result) == [track_id]
    assert result.complete is True
    assert result.unreachable == tuple(sorted(cluster.stores[:down]))
    client.withdraw(track_id, "sin licencia")
    assert ids(client.search("mar")) == []
    assert client.search("mar").complete is True


def test_upload_confirms_every_object_with_n_replicas(cluster, client, song):
    track_id = client.upload(song, md())
    manifest = decode_object(cluster.get(track_id, deadline=1e18, op_id="t"))
    assert isinstance(manifest, Manifest)
    for obj_id in [track_id, manifest.init, manifest.original, *(s.id for s in manifest.segments)]:
        assert len(cluster.holders(obj_id)) == 3
    assert manifest.original == sha256_hex(song.read_bytes())


def test_search_result_fields(client, song):
    track_id = client.upload(song, md())
    [track] = client.list().tracks
    assert (track.track_id, track.title, track.artist, track.license) == (track_id, "Canción del mar", "Ana", LICENSE)
    assert track.duration == pytest.approx(6.0)  # 3 fake segments of 2 s


def test_results_are_ordered_by_artist_title_and_id(client, tmp_path):
    for i, (title, artist) in enumerate([("Zeta", "Bea"), ("Alfa", "Bea"), ("Mar", "Ana")]):
        path = tmp_path / f"{i}.wav"
        path.write_bytes(f"audio {i}".encode() * 20)
        client.upload(path, md(title, artist))
    assert [(t.artist, t.title) for t in client.list().tracks] == [("Ana", "Mar"), ("Bea", "Alfa"), ("Bea", "Zeta")]


# --- with 3 stores down ---------------------------------------------------


def test_with_three_stores_down(cluster, client, song, other_song):
    kept = client.upload(song, md())
    cluster.down("store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        client.upload(other_song, md("Otra"))
    with pytest.raises(Unavailable):
        client.withdraw(kept, "sin licencia")
    result = client.search("")
    assert result.complete is False
    assert result.unreachable == ("store-1", "store-2", "store-3")
    # Whatever the two remaining stores hold is still returned.
    assert set(ids(result)) <= {kept}


def test_upload_writes_nothing_when_withdrawal_cannot_be_ruled_out(cluster, client, song):
    cluster.down("store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        client.upload(song, md())
    assert cluster.all_ids() == set()


# --- repeated and interrupted uploads ------------------------------------


def test_repeating_an_upload_gives_the_same_id_and_writes_nothing_new(cluster, client, song):
    first = client.upload(song, md())
    stored = {name: dict(cluster.store(name).objects) for name in cluster.stores}
    assert client.upload(song, md()) == first
    assert {name: dict(cluster.store(name).objects) for name in cluster.stores} == stored


def test_same_title_written_differently_is_the_same_track(client, song):
    import unicodedata

    assert client.upload(song, md("Café")) == client.upload(song, md(unicodedata.normalize("NFD", " Café ")))


def test_failed_manifest_write_leaves_the_track_visible_and_repeating_confirms_it(cluster, song):
    client = Client(cluster, encoder=fake_encoder)
    track_id = client.upload(song, md())  # learn the id the upload produces
    for name in cluster.stores:
        cluster.store(name).objects.pop(track_id, None)

    cluster.fail_put_after(track_id, 1)
    with pytest.raises(Unavailable):
        client.upload(song, md())
    assert len(cluster.holders(track_id)) == 1
    assert ids(client.search("mar")) == [track_id]  # visible, not confirmed

    assert client.upload(song, md()) == track_id
    assert len(cluster.holders(track_id)) == 3  # confirmed


def test_identical_segments_are_written_once(cluster, tmp_path):
    def silence(path, config, timeout):
        return EncodedAudio(init=b"init", segments=((b"silence", 2000),) * 4)

    path = tmp_path / "quiet.wav"
    path.write_bytes(b"quiet")
    client = Client(cluster, encoder=silence)
    track_id = client.upload(path, md("Silencio"))
    manifest = decode_object(cluster.get(track_id, deadline=1e18, op_id="t"))
    assert len(manifest.segments) == 4 and len({s.id for s in manifest.segments}) == 1
    assert sum(1 for _, oid in cluster.puts if oid == manifest.segments[0].id) == 3


# --- withdrawal -----------------------------------------------------------


def test_uploading_a_withdrawn_track_again_is_a_conflict_and_writes_nothing(cluster, client, song):
    track_id = client.upload(song, md())
    client.withdraw(track_id, "sin licencia")
    writes = len(cluster.puts)
    with pytest.raises(Conflict):
        client.upload(song, md())
    with pytest.raises(Conflict):
        client.upload(song, md("Otro título", "Otro artista"))  # same original, other metadata
    assert len(cluster.puts) == writes


def test_withdrawing_hides_every_track_with_the_same_original(client, song):
    a = client.upload(song, md("Canción del mar"))
    b = client.upload(song, md("Song of the sea"))
    assert sorted(ids(client.list())) == sorted([a, b])
    client.withdraw(a, "sin licencia")
    assert ids(client.list()) == []


def test_withdrawing_twice_does_not_write_a_second_tombstone(cluster, client, song):
    track_id = client.upload(song, md())
    client.withdraw(track_id, "sin licencia")
    writes = len(cluster.puts)
    client.withdraw(track_id, "otro motivo")
    assert len(cluster.puts) == writes
    tombstones = [o for o in (decode_object(d) for n in cluster.stores for d in cluster.store(n).objects.values())
                  if isinstance(o, Tombstone)]
    assert {t.reason for t in tombstones} == {"sin licencia"}


def test_tombstone_goes_to_every_store(cluster, client, song):
    track_id = client.upload(song, md())
    client.withdraw(track_id, "sin licencia")
    [tid] = {oid for name in cluster.stores for oid, data in cluster.store(name).objects.items()
             if isinstance(decode_object(data), Tombstone)}
    assert sorted(cluster.holders(tid)) == sorted(cluster.stores)


def test_withdraw_errors(cluster, client, song):
    with pytest.raises(Invalid):
        client.withdraw("not-an-id", "x")
    track_id = client.upload(song, md())
    with pytest.raises(Invalid):
        client.withdraw(track_id, "   ")
    with pytest.raises(NotFound):
        client.withdraw("f" * 64, "x")
    manifest = decode_object(cluster.get(track_id, deadline=1e18, op_id="t"))
    with pytest.raises(NotFound):
        client.withdraw(manifest.segments[0].id, "x")  # an id that is not a track
    cluster.down("store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        client.withdraw("f" * 64, "x")  # absence cannot be asserted


# --- validation -----------------------------------------------------------


def test_invalid_uploads_write_nothing(cluster, client, song, tmp_path):
    with pytest.raises(Invalid):
        client.upload(song, {"title": "", "artist": "Ana", "license": LICENSE})
    with pytest.raises(Invalid):
        client.upload(song, {**md(), "duration": 3})
    with pytest.raises(Invalid):
        client.upload(tmp_path / "missing.wav", md())
    small = Client(cluster, ClientConfig(max_upload_bytes=10), encoder=fake_encoder)
    with pytest.raises(Invalid):
        small.upload(song, md())
    assert cluster.all_ids() == set()


def test_errors_carry_the_operation_id(client, song):
    with pytest.raises(Invalid) as info:
        client.upload(song, {"title": "", "artist": "Ana", "license": LICENSE})
    assert info.value.op_id


def test_long_query_is_invalid(client):
    with pytest.raises(Invalid):
        client.search("x" * 201)


def test_config_from_env():
    config = load_client_config({"DTN_SEARCH_TIMEOUT_MS": "500", "DTN_UPLOAD_PARALLELISM": "2"})
    assert config.search_timeout_ms == 500 and config.upload_parallelism == 2
    assert config.upload_timeout_seconds == ClientConfig().upload_timeout_seconds


# --- end to end with the real encoder -------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_real_upload_with_ffmpeg_is_idempotent(cluster, tmp_path):
    path = tmp_path / "tone.flac"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
                    str(path)], check=True)
    client = Client(cluster)
    track_id = client.upload(path, md("Tono", "Prueba"))
    assert client.upload(path, md("Tono", "Prueba")) == track_id
    manifest = decode_object(cluster.get(track_id, deadline=1e18, op_id="t"))
    assert len(manifest.segments) == 3 and abs(manifest.duration_ms - 5000) < 50
    assert json.loads(cluster.get(track_id, deadline=1e18, op_id="t"))["type"] == "manifest"


def test_uploading_a_directory_is_invalid(cluster, client, tmp_path):
    with pytest.raises(Invalid):
        client.upload(tmp_path, md())
    assert cluster.all_ids() == set()


def test_reason_that_is_not_utf8_is_invalid(client, song):
    track_id = client.upload(song, md())
    with pytest.raises(Invalid):
        client.withdraw(track_id, "x\udcff")


def test_concurrent_uploads_and_searches(cluster, client, tmp_path):
    import threading

    files = []
    for i in range(10):
        path = tmp_path / f"{i}.wav"
        path.write_bytes(f"audio {i}".encode() * 50)
        files.append(path)
    errors, ids_seen = [], []

    def upload(path):
        try:
            ids_seen.append(client.upload(path, md(path.stem, "Ana")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def search():
        for _ in range(50):
            try:
                client.list()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=upload, args=(p,)) for p in files * 2]
    threads += [threading.Thread(target=search) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(ids_seen)) == 10 and len(client.list().tracks) == 10


def test_a_failed_write_stops_the_remaining_ones(cluster, tmp_path):
    def many_segments(path, config, timeout):
        return EncodedAudio(init=b"init", segments=tuple((f"seg {i}".encode(), 2000) for i in range(50)))

    path = tmp_path / "long.wav"
    path.write_bytes(b"long")
    cluster.fail_put_after(sha256_hex(b"long"), 0)  # the original is submitted first and fails at once
    client = Client(cluster, ClientConfig(upload_parallelism=1), encoder=many_segments)
    with pytest.raises(Unavailable):
        client.upload(path, md("Larga"))
    assert len(cluster.all_ids()) < 52  # not every object was written
    assert not any(isinstance(decode_object(d), Manifest)
                   for n in cluster.stores for d in cluster.store(n).objects.values())


def test_reason_longer_than_the_text_limit_is_invalid(cluster, client, song):
    track_id = client.upload(song, md())
    with pytest.raises(Invalid):
        client.withdraw(track_id, "x" * 201)
    client.withdraw(track_id, "x" * 200)
