"""The promise of ``dtn/cluster.py``, checked on both implementations.

``FakeCluster`` is what the track operations are tested against;
``RayCluster`` is what runs. The same tests run on both, so neither can drift
from the interface without a test failing. The Ray stores use the real codec
of ``dtn.objects``.
"""

import time
import uuid

import pytest
import ray

from dtn import admin
from dtn.config import StorageConfig
from dtn.errors import Invalid, NotFound, Unavailable
from dtn.fake_cluster import FakeCluster
from dtn.ids import sha256_hex
from dtn.placement import placement_order
from dtn.ray_cluster import RayCluster


class FakeHarness:
    def __init__(self):
        self.cluster = FakeCluster()

    def take_down(self, *names):
        self.cluster.down(*names)

    def holders(self, obj_id):
        return sorted(self.cluster.holders(obj_id))

    def close(self):
        pass


class RayHarness:
    def __init__(self, data_dir):
        self.namespace = f"contract-{uuid.uuid4().hex[:8]}"
        self.config = StorageConfig(data_dir=data_dir)
        self.handles = admin.start_stores(self.config, namespace=self.namespace)
        self.cluster = RayCluster(self.handles, self.config)
        self.down = set()

    def take_down(self, *names):
        """Remove stores for good, and wait until they stop answering."""
        for name in names:
            ray.kill(self.handles[name], no_restart=True)
            self.down.add(name)
        for name in names:
            limit = time.monotonic() + 10
            while True:
                try:
                    ray.get(self.handles[name].ping.remote(), timeout=2)
                except ray.exceptions.RayActorError:
                    break
                assert time.monotonic() < limit, f"{name} is still answering"
                time.sleep(0.05)

    def holders(self, obj_id):
        found = []
        for name, handle in self.handles.items():
            if name not in self.down and any(
                e["id"] == obj_id for e in ray.get(handle.inventory.remote())
            ):
                found.append(name)
        return sorted(found)

    def close(self):
        self.cluster.close()
        admin.stop_stores(self.config, namespace=self.namespace)


@pytest.fixture(params=["fake", "ray"])
def harness(request, tmp_path):
    if request.param == "fake":
        h = FakeHarness()
    else:
        request.getfixturevalue("ray_runtime")
        h = RayHarness(tmp_path)
    yield h
    h.close()


def soon(seconds=20.0):
    return time.monotonic() + seconds


def obj(text):
    data = text.encode()
    return sha256_hex(data), data


# --- put and get ----------------------------------------------------------------


def test_put_places_n_replicas_on_the_first_stores_of_the_order(harness):
    obj_id, data = obj("segment")
    harness.cluster.put(obj_id, data, deadline=soon(), op_id="op-put")
    assert harness.holders(obj_id) == sorted(placement_order(obj_id, harness.cluster.stores)[:3])


def test_get_returns_the_stored_bytes(harness):
    obj_id, data = obj("segment")
    harness.cluster.put(obj_id, data, deadline=soon(), op_id="op-put")
    assert harness.cluster.get(obj_id, deadline=soon(), op_id="op-get") == data


def test_put_rejects_data_that_does_not_match_its_id(harness):
    with pytest.raises(Invalid):
        harness.cluster.put("0" * 64, b"other", deadline=soon(), op_id="op-bad")


def test_with_two_stores_down_put_and_get_still_work(harness):
    obj_id, data = obj("survives two failures")
    order = placement_order(obj_id, harness.cluster.stores)
    harness.take_down(order[0], order[1])

    harness.cluster.put(obj_id, data, deadline=soon(), op_id="op-put")

    assert harness.holders(obj_id) == sorted(order[2:5])
    assert harness.cluster.get(obj_id, deadline=soon(), op_id="op-get") == data


# --- NotFound or Unavailable -------------------------------------------------------


def test_missing_object_is_not_found_with_fewer_than_n_stores_down(harness):
    harness.take_down("store-1", "store-2")
    with pytest.raises(NotFound):
        harness.cluster.get(sha256_hex(b"never stored"), deadline=soon(), op_id="op-get")


def test_with_n_stores_down_absence_cannot_be_claimed(harness):
    harness.take_down("store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        harness.cluster.get(sha256_hex(b"never stored"), deadline=soon(), op_id="op-get")


def test_with_n_stores_down_put_is_uncertain(harness):
    harness.take_down("store-1", "store-2", "store-3")
    obj_id, data = obj("cannot get three replicas")
    with pytest.raises(Unavailable):
        harness.cluster.put(obj_id, data, deadline=soon(), op_id="op-put")


# --- put_everywhere ---------------------------------------------------------------


def test_put_everywhere_reaches_every_live_store(harness):
    harness.take_down("store-5")
    obj_id, data = obj("tombstone")
    harness.cluster.put_everywhere(obj_id, data, deadline=soon(), op_id="op-tomb")
    assert harness.holders(obj_id) == ["store-1", "store-2", "store-3", "store-4"]


def test_put_everywhere_with_n_stores_down_is_uncertain(harness):
    harness.take_down("store-1", "store-2", "store-3")
    obj_id, data = obj("tombstone")
    with pytest.raises(Unavailable):
        harness.cluster.put_everywhere(obj_id, data, deadline=soon(), op_id="op-tomb")


# --- scatter ----------------------------------------------------------------------


def test_scatter_reports_answers_and_unreachable_stores(harness):
    harness.take_down("store-2")
    result = harness.cluster.scatter("search", "", deadline=soon(5), op_id="op-search")
    assert result.unreachable == ("store-2",)
    assert sorted(result.responses) == ["store-1", "store-3", "store-4", "store-5"]


def test_scatter_only_accepts_store_queries(harness):
    with pytest.raises(Invalid):
        harness.cluster.scatter("delete_if_older", "0" * 64, 0, deadline=soon(), op_id="op-x")
