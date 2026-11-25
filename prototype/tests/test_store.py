"""StoreCore without Ray: durability, integrity, indexing, saturation and ages.

The real manifest format belongs to ``dtn.objects``; these tests use the
stand-in codec of ``codec_stub`` so the store can be tested on its own.
"""

import threading

import pytest

from codec_stub import OTHER_ID, decode, encode, make_manifest, matches, validate_query
from dtn.errors import Corrupt, Invalid, NotFound, Unavailable
from dtn.ids import sha256_hex
from dtn.objects import Tombstone
from dtn.store import StoreCore


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def stored(obj_or_bytes) -> tuple[str, bytes]:
    data = obj_or_bytes if isinstance(obj_or_bytes, bytes) else encode(obj_or_bytes)
    return sha256_hex(data), data


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def open_store(tmp_path, clock):
    """Open (or reopen, simulating a restart) the store kept in ``tmp_path``."""

    def _open(max_in_flight=8, decode_fn=decode):
        return StoreCore(
            "store-1",
            tmp_path,
            max_in_flight=max_in_flight,
            decode=decode_fn,
            validate_query=validate_query,
            matches=matches,
            cluster_params={"num_stores": 5, "replicas": 3},
            clock=clock,
        )

    return _open


# --- put and get --------------------------------------------------------


def test_put_then_get_returns_the_same_bytes(open_store):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    assert store.put(obj_id, data) == {"stored": "new"}
    assert store.get(obj_id) == data


def test_putting_an_existing_object_does_not_rewrite_it(open_store):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    assert store.put(obj_id, data) == {"stored": "existing"}


def test_put_rejects_data_that_does_not_match_its_id(open_store, tmp_path):
    store = open_store()
    with pytest.raises(Invalid):
        store.put(OTHER_ID, b"something else")
    assert list((tmp_path / "objects").iterdir()) == []


def test_malformed_id_is_rejected_before_touching_the_disk(open_store):
    store = open_store()
    with pytest.raises(Invalid):
        store.get("../../etc/passwd")


def test_get_of_a_missing_object_is_not_found(open_store):
    with pytest.raises(NotFound):
        open_store().get(OTHER_ID)


# --- restart --------------------------------------------------------------


def test_objects_and_indexes_survive_a_restart(open_store):
    store = open_store()
    seg_id, seg = stored(b"segment bytes")
    man_id, man = stored(make_manifest("Lluvia", "Ana"))
    tomb_id, tomb = stored(Tombstone(man_id, OTHER_ID, ""))
    for obj_id, data in ((seg_id, seg), (man_id, man), (tomb_id, tomb)):
        store.put(obj_id, data)

    restarted = open_store()

    assert restarted.get(seg_id) == seg
    assert [m["track_id"] for m in restarted.search("lluvia")["manifests"]] == [man_id]
    assert len(restarted.tombstones(man_id, OTHER_ID)) == 1
    assert restarted.incarnation != store.incarnation


def test_half_written_files_are_discarded_at_startup(open_store, tmp_path):
    open_store()
    (tmp_path / "tmp" / "abc.partial").write_bytes(b"half")
    store = open_store()
    assert list((tmp_path / "tmp").iterdir()) == []
    assert store.inventory() == []


# --- integrity --------------------------------------------------------------


def test_damaged_copy_is_quarantined_on_read(open_store, tmp_path):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    (tmp_path / "objects" / obj_id).write_bytes(b"bit rot")

    with pytest.raises(Corrupt):
        store.get(obj_id)
    with pytest.raises(NotFound):
        store.get(obj_id)
    assert (tmp_path / "quarantine" / obj_id).exists()
    assert store.inventory() == []


def test_put_replaces_a_damaged_copy_instead_of_confirming_it(open_store, tmp_path):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    (tmp_path / "objects" / obj_id).write_bytes(b"bit rot")

    assert store.put(obj_id, data) == {"stored": "new"}
    assert store.get(obj_id) == data


def test_scrub_finds_damage_nobody_has_read(open_store, tmp_path):
    store = open_store()
    good_id, good = stored(b"good")
    bad_id, bad = stored(b"bad")
    store.put(good_id, good)
    store.put(bad_id, bad)
    (tmp_path / "objects" / bad_id).write_bytes(b"bit rot")

    assert store.scrub() == [bad_id]
    assert [e["id"] for e in store.inventory()] == [good_id]


def test_damaged_manifest_is_not_indexed_after_a_restart(open_store, tmp_path):
    store = open_store()
    man_id, man = stored(make_manifest("Lluvia", "Ana"))
    store.put(man_id, man)
    (tmp_path / "objects" / man_id).write_bytes(b'{"type": "manifest", "damaged": true}')

    restarted = open_store()
    assert restarted.search("")["manifests"] == []
    assert (tmp_path / "quarantine" / man_id).exists()


# --- search and tombstones --------------------------------------------------------


def test_inventory_tells_manifests_tombstones_and_data_apart(open_store):
    store = open_store()
    man_id, man = stored(make_manifest("Lluvia", "Ana"))
    tomb_id, tomb = stored(Tombstone(OTHER_ID, OTHER_ID, ""))
    seg_id, seg = stored(b"audio")
    for obj_id, data in ((man_id, man), (tomb_id, tomb), (seg_id, seg)):
        store.put(obj_id, data)

    kinds = {e["id"]: e["kind"] for e in store.inventory()}
    assert kinds == {man_id: "manifest", tomb_id: "tombstone", seg_id: "data"}


def test_search_filters_manifests_and_returns_every_tombstone(open_store):
    store = open_store()
    a_id, a = stored(make_manifest("Lluvia", "Ana"))
    b_id, b = stored(make_manifest("Sol", "Beto"))
    t_id, t = stored(Tombstone(OTHER_ID, OTHER_ID, ""))
    for obj_id, data in ((a_id, a), (b_id, b), (t_id, t)):
        store.put(obj_id, data)

    result = store.search("ana")
    assert [m["track_id"] for m in result["manifests"]] == [a_id]
    assert result["tombstoned"] == [{"track_id": OTHER_ID, "original": OTHER_ID}]


def test_search_rejects_an_overlong_query(open_store):
    with pytest.raises(Invalid):
        open_store().search("x" * 201)


def test_tombstones_are_found_by_track_or_by_original(open_store):
    store = open_store()
    track_id, original = sha256_hex(b"track"), sha256_hex(b"original")
    t_id, t = stored(Tombstone(track_id, original, ""))
    store.put(t_id, t)

    assert len(store.tombstones(track_id, OTHER_ID)) == 1
    assert len(store.tombstones(OTHER_ID, original)) == 1
    assert store.tombstones(OTHER_ID, OTHER_ID) == []


# --- ages and conditional delete ---------------------------------------------------


def test_age_counts_from_the_last_write(open_store, clock):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    clock.now += 50
    assert store.inventory()[0]["age_seconds"] == 50


def test_delete_if_older_keeps_recent_objects_and_deletes_old_ones(open_store, clock):
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)

    clock.now += 100
    assert store.delete_if_older(obj_id, 100) == {"deleted": False, "reason": "recent"}
    clock.now += 1
    assert store.delete_if_older(obj_id, 100) == {"deleted": True, "reason": "deleted"}
    assert store.delete_if_older(obj_id, 100) == {"deleted": False, "reason": "absent"}
    with pytest.raises(NotFound):
        store.get(obj_id)


def test_rewriting_an_object_renews_its_age(open_store, clock):
    """An upload that reuses an old unused segment must not lose it."""
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    clock.now += 1000
    store.put(obj_id, data)
    assert store.delete_if_older(obj_id, 100) == {"deleted": False, "reason": "recent"}


def test_a_put_racing_a_delete_never_loses_the_object(open_store, clock):
    """The repair agent decides to delete an old unused object while an upload
    writes it again. Whatever the interleaving, the object survives: either
    the rewrite renews its age first and the delete keeps it, or the delete
    goes first and the put writes it again before confirming."""
    store = open_store()
    for i in range(200):
        obj_id, data = stored(f"segment {i}".encode())
        store.put(obj_id, data)
        clock.now += 1000  # old enough to be deleted
        start = threading.Barrier(2)
        results = {}

        def rewrite():
            start.wait()
            results["put"] = store.put(obj_id, data)

        def delete():
            start.wait()
            results["delete"] = store.delete_if_older(obj_id, 100)

        threads = [threading.Thread(target=rewrite), threading.Thread(target=delete)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.get(obj_id) == data, results


def test_ages_restart_from_zero_after_a_restart(open_store, clock):
    """Age is measured on a monotonic clock that does not survive a restart:
    after one, every object is treated as just written (the safe direction)."""
    store = open_store()
    obj_id, data = stored(b"segment bytes")
    store.put(obj_id, data)
    clock.now += 1000

    restarted = open_store()
    assert restarted.inventory()[0]["age_seconds"] == 0
    assert restarted.delete_if_older(obj_id, 100)["reason"] == "recent"


def test_deleted_tombstone_leaves_no_trace_in_the_index(open_store, clock):
    store = open_store()
    track_id = sha256_hex(b"track")
    t_id, t = stored(Tombstone(track_id, OTHER_ID, ""))
    store.put(t_id, t)
    clock.now += 10
    store.delete_if_older(t_id, 1)
    assert store.tombstones(track_id, OTHER_ID) == []


# --- saturation ------------------------------------------------------------


def test_store_rejects_requests_beyond_its_limit_and_recovers(open_store):
    entered, release = threading.Event(), threading.Event()

    def blocking_decode(data):
        entered.set()
        release.wait(5)
        return None

    store = open_store(max_in_flight=1, decode_fn=blocking_decode)
    obj_id, data = stored(b"slow to index")
    worker = threading.Thread(target=store.put, args=(obj_id, data))
    worker.start()
    assert entered.wait(5)

    with pytest.raises(Unavailable) as excinfo:
        store.ping()
    assert excinfo.value.saturated is True

    release.set()
    worker.join(5)
    assert store.ping()["objects"] == 1


def test_ping_reports_name_incarnation_object_count_and_cluster_params(open_store):
    store = open_store()
    obj_id, data = stored(b"x")
    store.put(obj_id, data)
    reply = store.ping()
    assert reply == {
        "store": "store-1",
        "incarnation": store.incarnation,
        "objects": 1,
        "num_stores": 5,
        "replicas": 3,
    }
