"""Repair agent over real store actors: restoring replicas and collecting garbage."""

import os
import signal
import time
import uuid

import pytest
import ray

import codec_stub
from dtn import admin
from dtn.config import StorageConfig
from dtn.ids import sha256_hex
from dtn.objects import Tombstone
from dtn.placement import placement_order
from dtn.ray_cluster import RayCluster
from dtn.repair import Repairer
from dtn.store_actor import Codec

ray.cloudpickle.register_pickle_by_value(codec_stub)
CODEC = Codec(codec_stub.decode, codec_stub.validate_query, codec_stub.matches)
STORES = ("store-1", "store-2", "store-3", "store-4", "store-5")


def soon(seconds=20.0):
    return time.monotonic() + seconds


def obj(content) -> tuple[str, bytes]:
    data = content if isinstance(content, bytes) else codec_stub.encode(content)
    return sha256_hex(data), data


def holders(handles, obj_id):
    found = []
    for name, handle in handles.items():
        if any(e["id"] == obj_id for e in ray.get(handle.inventory.remote())):
            found.append(name)
    return sorted(found)


def wait_until(condition, seconds=15.0):
    limit = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < limit, "condition never became true"
        time.sleep(0.2)


@pytest.fixture
def setup(ray_runtime, tmp_path):
    """Five real stores, a client cluster and a repairer, with config overrides."""
    namespace = f"repair-{uuid.uuid4().hex[:8]}"
    made = {}

    def _start(**overrides):
        config = StorageConfig(data_dir=tmp_path, **overrides)
        handles = admin.start_stores(config, namespace=namespace, codec=CODEC)
        cluster = RayCluster(handles, config)
        made.update(config=config, cluster=cluster)
        return handles, cluster, Repairer(cluster, config), config, namespace

    yield _start
    made["cluster"].close()
    admin.stop_stores(made["config"], namespace=namespace)


def track(handles, cluster, *segments, title="Lluvia", original=None):
    """Store the segments and a manifest that uses them. Returns the track id."""
    for seg_id, data in segments:
        cluster.put(seg_id, data, deadline=soon(), op_id="op-upload")
    original = original or segments[0][0]
    man_id, man = obj(
        codec_stub.make_manifest(
            title, "Ana", init=segments[0][0], original=original, segments=[s for s, _ in segments]
        )
    )
    cluster.put(man_id, man, deadline=soon(), op_id="op-upload")
    return man_id


def lose_replica(handles, store, obj_id, tmp_path):
    """The store loses its copy: the file goes, then a crash rebuilds the index."""
    (tmp_path / store / "objects" / obj_id).unlink()
    os.kill(ray.get(handles[store].process_id.remote()), signal.SIGKILL)
    wait_until(lambda: _pings(handles[store]))


def take_down(handles, *names):
    """Remove stores for good, and wait until they stop answering:
    ``ray.kill`` returns before the process is gone."""
    for name in names:
        ray.kill(handles[name], no_restart=True)
    for name in names:
        wait_until(lambda: not _pings(handles[name]), seconds=10)


def _pings(handle):
    try:
        ray.get(handle.ping.remote(), timeout=2)
        return True
    except Exception:
        return False


# --- restoring replicas -------------------------------------------------------


def test_lost_replica_returns_to_its_place_in_the_order(setup, tmp_path):
    handles, cluster, repairer, _, _ = setup()
    seg = obj(b"segment")
    track(handles, cluster, seg)
    preferred = placement_order(seg[0], STORES)[0]
    lose_replica(handles, preferred, seg[0], tmp_path)

    report = repairer.run_round()

    assert report.copies_made == 1
    assert holders(handles, seg[0]) == sorted(placement_order(seg[0], STORES)[:3])


def test_with_a_store_down_replicas_move_to_the_next_stores_in_order(setup):
    """This agent never saw the store answering, so it knows nothing of what
    the store holds and gives it up at once."""
    handles, cluster, repairer, _, _ = setup()
    seg = obj(b"segment")
    track(handles, cluster, seg)
    order = placement_order(seg[0], STORES)
    take_down(handles, order[0])

    report = repairer.run_round()

    assert report.unreachable == (order[0],)
    live = {n: h for n, h in handles.items() if n != order[0]}
    assert holders(live, seg[0]) == sorted(order[1:4])


def test_a_silent_store_keeps_its_replicas_until_it_is_lost(setup):
    """A short outage causes no copies: until DTN_STORE_LOST_SECONDS pass,
    the replicas of the silent store's last inventory still count."""
    handles, cluster, repairer, _, _ = setup(store_lost_seconds=1.0)
    seg = obj(b"segment")
    man_id = track(handles, cluster, seg)
    order = placement_order(seg[0], STORES)
    on_silent = [i for i in (seg[0], man_id) if order[0] in holders(handles, i)]
    repairer.run_round()  # sees every store and remembers its inventory
    take_down(handles, order[0])

    waiting = repairer.run_round()
    assert (waiting.presumed, waiting.lost, waiting.copies_made) == ((order[0],), (), 0)
    assert not repairer.loss_pending()

    time.sleep(1.1)
    assert repairer.loss_pending()
    given_up = repairer.run_round()

    assert (given_up.presumed, given_up.lost) == ((), (order[0],))
    assert given_up.copies_made == len(on_silent)
    live = {n: h for n, h in handles.items() if n != order[0]}
    assert holders(live, seg[0]) == sorted(order[1:4])
    assert not repairer.loss_pending()


def test_tombstones_are_copied_to_every_store(setup):
    handles, cluster, repairer, _, _ = setup()
    tomb_id, tomb = obj(Tombstone(codec_stub.OTHER_ID, codec_stub.OTHER_ID, ""))
    cluster.put(tomb_id, tomb, deadline=soon(), op_id="op-withdraw")  # only N copies

    repairer.run_round()

    assert holders(handles, tomb_id) == list(STORES)


def test_unused_objects_are_not_restored(setup):
    """With every store answering, repair only keeps what some track uses."""
    handles, cluster, repairer, _, _ = setup()
    used, orphan = obj(b"used"), obj(b"orphan")
    for seg in (used, orphan):  # one replica each, short of N
        ray.get(handles["store-1"].put.remote(*seg))
    man_id, man = obj(
        codec_stub.make_manifest("Lluvia", "Ana", init=used[0], original=used[0], segments=[used[0]])
    )
    cluster.put(man_id, man, deadline=soon(), op_id="op-upload")

    repairer.run_round()

    assert len(holders(handles, used[0])) == 3
    assert holders(handles, orphan[0]) == ["store-1"]
    assert len(holders(handles, man_id)) == 3


def test_a_second_round_finds_nothing_to_do(setup, tmp_path):
    handles, cluster, repairer, _, _ = setup()
    seg = obj(b"segment")
    track(handles, cluster, seg)
    lose_replica(handles, placement_order(seg[0], STORES)[1], seg[0], tmp_path)
    assert repairer.run_round().copies_made == 1

    again = repairer.run_round()

    assert (again.copies_made, again.copies_failed, again.deleted) == (0, 0, 0)


# --- collecting garbage -------------------------------------------------------


def test_unused_and_withdrawn_audio_is_deleted_after_the_grace_period(setup):
    handles, cluster, repairer, _, _ = setup(gc_grace_seconds=1.0)
    kept, orphan, withdrawn, same_recording = (obj(b"kept"), obj(b"orphan"), obj(b"gone"), obj(b"copy"))
    kept_track = track(handles, cluster, kept)
    cluster.put(*orphan, deadline=soon(), op_id="op-abandoned-upload")
    gone_track = track(handles, cluster, withdrawn, title="Gone")
    # Another track of the same recording: withdrawn with it.
    other_track = track(handles, cluster, same_recording, title="Gone again", original=withdrawn[0])
    tomb_id, tomb = obj(Tombstone(gone_track, withdrawn[0], ""))
    cluster.put_everywhere(tomb_id, tomb, deadline=soon(), op_id="op-withdraw")
    time.sleep(1.5)

    report = repairer.run_round()

    assert report.deleted == 9  # three objects, three replicas each
    for seg_id, _ in (orphan, withdrawn, same_recording):
        assert holders(handles, seg_id) == []
    assert len(holders(handles, kept[0])) == 3
    for kept_id in (kept_track, gone_track, other_track):  # manifests stay
        assert len(holders(handles, kept_id)) == 3
    assert holders(handles, tomb_id) == list(STORES)


def test_recent_unused_objects_are_kept(setup):
    """An upload in progress has segments and no manifest yet."""
    handles, cluster, repairer, _, _ = setup()  # grace: one day
    orphan = obj(b"segment of an upload in progress")
    cluster.put(*orphan, deadline=soon(), op_id="op-upload")

    report = repairer.run_round()

    assert report.deleted == 0
    assert report.kept_recent == 3
    assert len(holders(handles, orphan[0])) == 3


def test_nothing_is_deleted_while_a_store_does_not_answer(setup):
    """The silent store might hold the only manifest that uses the object."""
    handles, cluster, repairer, _, _ = setup(gc_grace_seconds=1.0)
    orphan = obj(b"orphan")
    cluster.put(*orphan, deadline=soon(), op_id="op-upload")
    silent = next(s for s in STORES if s not in holders(handles, orphan[0]))
    take_down(handles, silent)
    time.sleep(1.5)

    report = repairer.run_round()

    assert report.deleted == 0
    assert report.sweep_skipped == f"no answer from {silent}"
    live = {n: h for n, h in handles.items() if n != silent}
    assert len(holders(live, orphan[0])) == 3


# --- the agent as a Ray actor -------------------------------------------------


def test_agent_repairs_on_its_own_once_a_silent_store_is_lost(setup):
    """No periodic round is due: the store that stops answering triggers a
    round, and so does the moment it has been silent long enough to be lost."""
    handles, cluster, _, config, namespace = setup(
        repair_interval_seconds=600, timeout_max_ms=500, store_lost_seconds=1.0
    )
    agent = admin.start_repair(config, namespace=namespace)
    seg = obj(b"segment")
    track(handles, cluster, seg)
    ray.get(agent.run_now.remote())  # its last inventory now includes the segment
    order = placement_order(seg[0], STORES)

    take_down(handles, order[0])

    wait_until(lambda: order[3] in holders({s: handles[s] for s in order[1:]}, seg[0]))
    report = ray.get(agent.last_report.remote())
    assert (report.reason, report.lost) == ("store-lost", (order[0],))


def test_agent_comes_back_after_a_crash(setup):
    _, _, _, config, namespace = setup(timeout_max_ms=500)
    agent = admin.start_repair(config, namespace=namespace)
    first = ray.get(agent.process_id.remote())

    admin.crash_store("repair", namespace=namespace)

    wait_until(lambda: _pid(agent) not in (None, first))
    assert ray.get(agent.run_now.remote()).unreachable == ()


def _pid(agent):
    try:
        return ray.get(agent.process_id.remote(), timeout=2)
    except Exception:
        return None


# --- acceptance: repair end to end ---------------------------------------------


def test_after_losing_a_store_everything_is_back_to_n_replicas(setup):
    """Kill a store for good and wait: once it is lost, within two periods
    every object has N replicas again and the tombstone is on every live store."""
    handles, cluster, _, config, namespace = setup(
        repair_interval_seconds=1.0, store_lost_seconds=1.0, timeout_max_ms=500
    )
    agent = admin.start_repair(config, namespace=namespace)
    segments = [obj(f"segment {i}".encode()) for i in range(10)]
    track(handles, cluster, *segments)
    tomb_id, tomb = obj(Tombstone(codec_stub.OTHER_ID, codec_stub.OTHER_ID, ""))
    cluster.put(tomb_id, tomb, deadline=soon(), op_id="op-withdraw")
    ray.get(agent.run_now.remote())
    take_down(handles, "store-1")
    live = {n: h for n, h in handles.items() if n != "store-1"}

    def restored():
        if holders(live, tomb_id) != sorted(live):
            return False
        return all(len(holders(live, seg_id)) == 3 for seg_id, _ in segments)

    wait_until(restored, seconds=1.0 + 2 * 1.0 + 10)  # lost + two periods + slack


def test_an_unused_object_rewritten_during_the_grace_period_is_kept(setup):
    handles, cluster, repairer, _, _ = setup(gc_grace_seconds=1.0)
    orphan = obj(b"segment reused by a new upload")
    cluster.put(*orphan, deadline=soon(), op_id="op-first-upload")
    time.sleep(1.5)  # old enough to be deleted
    cluster.put(*orphan, deadline=soon(), op_id="op-second-upload")  # renews its age

    report = repairer.run_round()

    assert report.deleted == 0
    assert len(holders(handles, orphan[0])) == 3
