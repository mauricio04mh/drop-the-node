"""RayCluster over store actors: replicas, failures, hedging and suspicion.

Real store actors cover placement, crashes and saturation. Slow stand-in
actors cover timing: hedged reads, suspicion and readmission.
"""

import os
import signal
import time
import uuid

import pytest
import ray

import codec_stub
from dtn import admin
from dtn.config import StorageConfig
from dtn.errors import Invalid, NotFound, Unavailable
from dtn.ids import sha256_hex
from dtn.placement import placement_order
from dtn.ray_cluster import RayCluster
from dtn.store_actor import Codec

ray.cloudpickle.register_pickle_by_value(codec_stub)
CODEC = Codec(codec_stub.decode, codec_stub.validate_query, codec_stub.matches)


def soon(seconds=20.0):
    return time.monotonic() + seconds


def obj(text: str) -> tuple[str, bytes]:
    data = text.encode()
    return sha256_hex(data), data


def holders(handles, obj_id):
    """Stores whose inventory lists ``obj_id``."""
    found = []
    for name, handle in handles.items():
        if any(e["id"] == obj_id for e in ray.get(handle.inventory.remote())):
            found.append(name)
    return sorted(found)


def take_down(handles, *names):
    """Remove stores for good: they stay down, as after a lasting failure.

    ``ray.kill`` returns before the process is gone: wait until each store
    stops answering, or a call sent right after could still reach it.
    """
    for name in names:
        ray.kill(handles[name], no_restart=True)
    for name in names:
        limit = time.monotonic() + 10
        while True:
            try:
                ray.get(handles[name].ping.remote(), timeout=2)
            except ray.exceptions.RayActorError:
                break
            assert time.monotonic() < limit, f"{name} is still answering"
            time.sleep(0.05)


@pytest.fixture
def stores(ray_runtime, tmp_path):
    """Five real stores (S = 5, N = 3) and a RayCluster over them."""
    namespace = f"cluster-{uuid.uuid4().hex[:8]}"
    made = {}

    def _start(**overrides):
        config = StorageConfig(data_dir=tmp_path, **overrides)
        handles = admin.start_stores(config, namespace=namespace, codec=CODEC)
        cluster = RayCluster(handles, config)
        made.update(config=config, cluster=cluster)
        return handles, cluster

    yield _start
    made["cluster"].close()
    admin.stop_stores(made["config"], namespace=namespace)


# --- writes and reads ---------------------------------------------------------


def test_put_places_replicas_on_the_first_n_stores_of_the_order(stores):
    handles, cluster = stores()
    obj_id, data = obj("segment")

    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")

    assert holders(handles, obj_id) == sorted(placement_order(obj_id, cluster.stores)[:3])


def test_get_reads_from_the_preferred_store(stores):
    _, cluster = stores()
    obj_id, data = obj("segment")
    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")

    got, trace = cluster.get_traced(obj_id, deadline=soon(), op_id="op-get")

    assert got == data
    assert trace.store == trace.preferred == placement_order(obj_id, cluster.stores)[0]
    assert trace.reason is None


def test_put_rejects_data_that_does_not_match_its_id(stores):
    _, cluster = stores()
    with pytest.raises(Invalid):
        cluster.put("0" * 64, b"other", deadline=soon(), op_id="op-bad")


# --- stores down --------------------------------------------------------------


def test_with_two_stores_down_writes_and_reads_still_work(stores):
    handles, cluster = stores()
    obj_id, data = obj("survives two failures")
    order = placement_order(obj_id, cluster.stores)
    take_down(handles, order[0], order[1])

    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")
    got, trace = cluster.get_traced(obj_id, deadline=soon(), op_id="op-get")

    assert got == data
    assert trace.store in order[2:]
    assert trace.reason in ("suspect", "error")


def test_a_store_that_crashes_and_restarts_keeps_serving(stores):
    handles, cluster = stores()
    obj_id, data = obj("crash and restart")
    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")
    preferred = placement_order(obj_id, cluster.stores)[0]

    os.kill(ray.get(handles[preferred].process_id.remote()), signal.SIGKILL)

    assert cluster.get(obj_id, deadline=soon(), op_id="op-get") == data


def test_missing_object_is_not_found_with_two_stores_down(stores):
    handles, cluster = stores()
    take_down(handles, "store-1", "store-2")
    with pytest.raises(NotFound):
        cluster.get("0" * 64, deadline=soon(), op_id="op-missing")


def test_with_three_stores_down_absence_cannot_be_claimed(stores):
    handles, cluster = stores()
    take_down(handles, "store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        cluster.get("0" * 64, deadline=soon(), op_id="op-missing")


def test_with_three_stores_down_writes_fail_as_uncertain(stores):
    handles, cluster = stores()
    take_down(handles, "store-1", "store-2", "store-3")
    obj_id, data = obj("cannot get three replicas")
    with pytest.raises(Unavailable):
        cluster.put(obj_id, data, deadline=soon(), op_id="op-put")


def test_with_three_stores_down_scatter_lists_them_as_unreachable(stores):
    handles, cluster = stores()
    take_down(handles, "store-1", "store-2", "store-3")

    result = cluster.scatter("ping", deadline=soon(5), op_id="op-ping")

    assert result.unreachable == ("store-1", "store-2", "store-3")
    assert sorted(result.responses) == ["store-4", "store-5"]


@pytest.mark.parametrize("crashed", [0, 1, 2])
def test_with_stores_crashed_by_kill_9_the_contract_holds(stores, crashed):
    """The same checks with 0, 1 and 2 stores crashing for real right before
    the calls: they are restarting while the client writes and reads."""
    handles, cluster = stores()
    obj_id, data = obj(f"survives {crashed} crashes")
    victims = placement_order(obj_id, cluster.stores)[:crashed]
    for store in victims:
        os.kill(ray.get(handles[store].process_id.remote()), signal.SIGKILL)

    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")
    assert len(holders(handles, obj_id)) >= 3  # N confirmed replicas
    assert cluster.get(obj_id, deadline=soon(), op_id="op-get") == data
    with pytest.raises(NotFound):  # fewer than N unreachable: absence is certain
        cluster.get(sha256_hex(b"never stored"), deadline=soon(), op_id="op-missing")


def test_put_everywhere_reaches_every_live_store(stores):
    handles, cluster = stores()
    take_down(handles, "store-5")
    obj_id, data = obj("tombstone")

    cluster.put_everywhere(obj_id, data, deadline=soon(), op_id="op-tomb")

    assert holders({n: h for n, h in handles.items() if n != "store-5"}, obj_id) == [
        "store-1",
        "store-2",
        "store-3",
        "store-4",
    ]


# --- scatter ------------------------------------------------------------------


def test_scatter_reports_answers_and_unreachable_stores(stores):
    handles, cluster = stores()
    manifest = codec_stub.encode(codec_stub.make_manifest("Lluvia", "Ana"))
    cluster.put(sha256_hex(manifest), manifest, deadline=soon(), op_id="op-put")
    take_down(handles, "store-4")

    result = cluster.scatter("search", "lluvia", deadline=soon(5), op_id="op-search")

    assert result.unreachable == ("store-4",)
    found = sum(len(r["manifests"]) for r in result.responses.values())
    assert found == 3 - ("store-4" in placement_order(sha256_hex(manifest), cluster.stores)[:3])


def test_scatter_only_accepts_store_queries(stores):
    _, cluster = stores()
    with pytest.raises(Invalid):
        cluster.scatter("delete_if_older", "0" * 64, 0, deadline=soon(), op_id="op-x")


# --- saturation ---------------------------------------------------------------


def test_saturated_store_still_gets_its_replica(stores):
    """A write retries a saturated store instead of skipping it, so the
    replica lands where readers look first."""
    handles, cluster = stores(store_concurrency=1)
    i = 0
    while True:  # an object whose preferred store is store-1
        obj_id, data = obj(f"wants store-1 #{i}")
        if placement_order(obj_id, cluster.stores)[0] == "store-1":
            break
        i += 1
    slow = codec_stub.SLOW_MARKER + b"1.0"
    busy = handles["store-1"].put.remote(sha256_hex(slow), slow)
    time.sleep(0.3)

    cluster.put(obj_id, data, deadline=soon(), op_id="op-put")

    assert "store-1" in holders(handles, obj_id)
    assert not cluster.detector.is_suspect("store-1")
    ray.get(busy)


# --- timing: hedging, suspicion, readmission -----------------------------------


@ray.remote(max_concurrency=8)
class SlowStore:
    """Stand-in store whose reads take a configurable time."""

    def __init__(self, objects: dict, delay: float, put_delay: float = 0.0):
        self.objects = objects
        self.delay = delay
        self.put_delay = put_delay

    def set_delay(self, delay: float):
        self.delay = delay

    def get(self, obj_id, op_id=None):
        time.sleep(self.delay)
        if obj_id not in self.objects:
            raise NotFound("absent")
        return self.objects[obj_id]

    def put(self, obj_id, data, op_id=None):
        time.sleep(self.put_delay)
        self.objects[obj_id] = data
        return {"stored": "new"}

    def keys(self):
        return sorted(self.objects)

    def ping(self, op_id=None):
        return {"store": "slow"}


@pytest.fixture
def slow_cluster(ray_runtime):
    clusters = []

    def _make(delays: dict, objects: dict, put_delays=None, **config):
        cfg = StorageConfig(num_stores=3, replicas=2, **config)
        put_delays = put_delays or {}
        handles = {
            name: SlowStore.remote(objects.get(name, {}), delays[name], put_delays.get(name, 0.0))
            for name in cfg.store_names
        }
        ray.get([h.ping.remote() for h in handles.values()])
        cluster = RayCluster(handles, cfg, probe_interval=0.2)
        clusters.append(cluster)
        return handles, cluster

    yield _make
    for c in clusters:
        c.close()


def test_slow_preferred_store_is_hedged(slow_cluster):
    obj_id, data = obj("hedge me")
    order = placement_order(obj_id, ("store-1", "store-2", "store-3"))
    delays = {order[0]: 1.5, order[1]: 0.0, order[2]: 0.0}
    _, cluster = slow_cluster(delays, {s: {obj_id: data} for s in order}, timeout_min_ms=100)

    start = time.monotonic()
    got, trace = cluster.get_traced(obj_id, deadline=soon(), op_id="op-hedge")

    assert got == data
    assert time.monotonic() - start < 0.8  # did not wait for the slow store
    assert trace.preferred == order[0]
    assert trace.store == order[1]
    assert trace.reason == "hedged"


def test_store_past_its_timeout_becomes_suspect_and_is_readmitted(slow_cluster):
    obj_id, data = obj("suspect me")
    order = placement_order(obj_id, ("store-1", "store-2", "store-3"))
    delays = {order[0]: 3.0, order[1]: 0.8, order[2]: 0.8}
    handles, cluster = slow_cluster(
        delays, {s: {obj_id: data} for s in order}, timeout_min_ms=100, timeout_max_ms=500
    )

    _, first = cluster.get_traced(obj_id, deadline=soon(), op_id="op-1")
    assert first.reason == "hedged"
    assert cluster.detector.is_suspect(order[0])

    _, second = cluster.get_traced(obj_id, deadline=soon(), op_id="op-2")
    assert second.reason == "suspect"  # skipped without waiting

    ray.get(handles[order[0]].set_delay.remote(0.0))
    limit = time.monotonic() + 10
    while cluster.detector.is_suspect(order[0]):
        assert time.monotonic() < limit, "suspect store was never readmitted"
        time.sleep(0.1)


def test_damaged_bytes_from_a_store_are_never_returned(slow_cluster):
    obj_id, data = obj("check every byte")
    order = placement_order(obj_id, ("store-1", "store-2", "store-3"))
    objects = {order[0]: {obj_id: b"bit rot"}, order[1]: {obj_id: data}, order[2]: {obj_id: data}}
    _, cluster = slow_cluster({s: 0.0 for s in order}, objects)

    got, trace = cluster.get_traced(obj_id, deadline=soon(), op_id="op-corrupt")

    assert got == data
    assert trace.reason == "corrupt"
    assert cluster.detector.is_suspect(order[0])


# --- write timeouts -------------------------------------------------------------


def test_write_timeout_grows_with_the_size_of_the_object():
    cluster = RayCluster({}, StorageConfig(min_write_mbps=10))
    try:
        small = cluster.write_timeout("store-1", 0)
        large = cluster.write_timeout("store-1", 20_000_000)
    finally:
        cluster.close()
    assert small == cluster.detector.timeout("store-1")
    assert large - small == pytest.approx(2.0)  # 20 MB at 10 MB/s


def test_a_store_too_slow_for_the_size_is_replaced_by_the_next_one(slow_cluster):
    """A store that is alive but hangs on a write does not hold the upload
    until its deadline: past its write timeout, the next store gets the replica."""
    obj_id, data = obj("write me")
    order = placement_order(obj_id, ("store-1", "store-2", "store-3"))
    handles, cluster = slow_cluster(
        {s: 0.0 for s in order},
        {},
        put_delays={order[0]: 5.0},
        timeout_min_ms=100,
        timeout_max_ms=300,
    )

    start = time.monotonic()
    cluster.put(obj_id, data, deadline=soon(), op_id="op-slow-write")

    assert time.monotonic() - start < 2.0  # did not wait the 5 s of the slow store
    for store in order[1:]:
        assert ray.get(handles[store].keys.remote()) == [obj_id]
