"""Stores as Ray actors: startup, the interface across processes, crash and
restart from disk, and rejection when saturated."""

import os
import subprocess
import sys
import time
import uuid

import pytest
import ray

import codec_stub
from dtn import admin
from dtn.config import StorageConfig
from dtn.errors import Invalid, NotFound, Unavailable
from dtn.ids import sha256_hex
from dtn.ray_cluster import RayCluster
from dtn.store_actor import Codec

# Ray pickles functions by reference (module + name), and its worker processes
# cannot import modules from tests/. Send this module's code along instead.
ray.cloudpickle.register_pickle_by_value(codec_stub)

CODEC = Codec(codec_stub.decode, codec_stub.validate_query, codec_stub.matches)


@pytest.fixture
def cluster(ray_runtime, tmp_path):
    """Start a small set of stores in a namespace of their own; remove them after."""
    namespace = f"stores-{uuid.uuid4().hex[:8]}"

    def _start(**overrides):
        settings = {"num_stores": 3, "replicas": 2, "data_dir": tmp_path, **overrides}
        config = StorageConfig(**settings)
        _start.config = config
        return admin.start_stores(config, namespace=namespace, codec=CODEC)

    _start.namespace = namespace
    yield _start
    admin.stop_stores(_start.config, namespace=namespace)


def wait_for_new_incarnation(handle, old, timeout=30.0):
    """Ping until the store answers from a new process."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            reply = ray.get(handle.ping.remote(), timeout=5)
            if reply["incarnation"] != old:
                return reply
        except ray.exceptions.RayActorError:
            pass
        assert time.monotonic() < deadline, "store did not come back"
        time.sleep(0.2)


def test_start_stores_starts_every_store_and_is_idempotent(cluster):
    handles = cluster()
    replies = ray.get([h.ping.remote() for h in handles.values()])
    assert [r["store"] for r in replies] == ["store-1", "store-2", "store-3"]

    again = admin.start_stores(cluster.config, namespace=cluster.namespace, codec=CODEC)
    assert ray.get([h.ping.remote() for h in again.values()]) == replies


def test_store_interface_works_across_processes(cluster):
    store = cluster()["store-1"]
    data = b"segment bytes"
    obj_id = sha256_hex(data)

    assert ray.get(store.put.remote(obj_id, data, "op-1")) == {"stored": "new"}
    assert ray.get(store.get.remote(obj_id, "op-2")) == data
    with pytest.raises(NotFound):
        ray.get(store.get.remote("0" * 64))
    with pytest.raises(Invalid):
        ray.get(store.put.remote(obj_id, b"other bytes"))


def test_each_store_keeps_its_objects_in_its_own_directory(cluster, tmp_path):
    handles = cluster()
    data = b"only in store-2"
    ray.get(handles["store-2"].put.remote(sha256_hex(data), data))

    assert (tmp_path / "store-2" / "objects" / sha256_hex(data)).exists()
    assert not (tmp_path / "store-1" / "objects" / sha256_hex(data)).exists()


def test_crashed_store_comes_back_with_its_data(cluster):
    """kill -9, automatic restart by Ray, index rebuilt from disk."""
    store = cluster()["store-1"]
    data = b"must survive the crash"
    obj_id = sha256_hex(data)
    ray.get(store.put.remote(obj_id, data))
    before = ray.get(store.ping.remote())

    pid = admin.crash_store("store-1", namespace=cluster.namespace)

    after = wait_for_new_incarnation(store, before["incarnation"])
    assert after["store"] == "store-1"
    assert after["objects"] == 1
    assert ray.get(store.process_id.remote()) != pid
    assert ray.get(store.get.remote(obj_id)) == data


def test_saturated_store_rejects_at_once_instead_of_queueing(cluster):
    """With its own limit full, the store answers "saturated" right away.

    Ray alone would queue the call; the extra actor threads let StoreCore
    run the rejection.
    """
    store = cluster(store_concurrency=1)["store-1"]
    slow = codec_stub.SLOW_MARKER + b"2"
    busy = store.put.remote(sha256_hex(slow), slow)
    time.sleep(0.5)

    start = time.monotonic()
    with pytest.raises(Unavailable) as excinfo:
        ray.get(store.ping.remote(), timeout=10)
    assert excinfo.value.saturated is True
    assert time.monotonic() - start < 1.0

    ray.get(busy, timeout=10)
    assert ray.get(store.ping.remote())["objects"] == 1


def test_status_reports_every_store(cluster):
    cluster()
    replies = admin.status(cluster.config, namespace=cluster.namespace)
    assert sorted(replies) == ["store-1", "store-2", "store-3"]
    assert all(isinstance(r, dict) for r in replies.values())


def test_crash_command_works_from_another_process(cluster):
    """The experiment script crashes a store from its own process."""
    store = cluster()["store-2"]
    before = ray.get(store.ping.remote())
    env = {**os.environ, "DTN_NUM_STORES": "3", "DTN_REPLICAS": "2"}

    result = subprocess.run(
        [sys.executable, "-m", "dtn.admin", "--namespace", cluster.namespace, "crash", "store-2"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "crashed store-2" in result.stdout
    wait_for_new_incarnation(store, before["incarnation"])


# --- one configuration for the whole system ------------------------------------


def test_data_directory_rejects_another_s_or_n_after_a_restart(cluster):
    cluster()  # records S = 3, N = 2 in the data directory
    admin.stop_stores(cluster.config, namespace=cluster.namespace)

    with pytest.raises(ValueError, match="cluster.json"):
        cluster(replicas=3)


def test_running_stores_reject_a_start_with_another_n(cluster, tmp_path):
    cluster()
    other_dir = tmp_path / "elsewhere"  # passes the data directory check

    with pytest.raises(ValueError, match="store-1 runs with"):
        cluster(replicas=3, data_dir=other_dir)


def test_client_with_another_n_cannot_connect(cluster):
    cluster()
    same = RayCluster.connect(cluster.config, namespace=cluster.namespace)
    same.close()

    other = StorageConfig(num_stores=3, replicas=3, data_dir=cluster.config.data_dir)
    with pytest.raises(ValueError, match="DTN_REPLICAS"):
        RayCluster.connect(other, namespace=cluster.namespace)
