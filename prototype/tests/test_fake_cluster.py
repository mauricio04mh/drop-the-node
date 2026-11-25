"""The in-memory cluster follows the semantics documented in dtn/cluster.py."""

import time

import pytest

from dtn.errors import Invalid, NotFound, Unavailable
from dtn.fake_cluster import FakeCluster
from dtn.placement import placement_order
from dtn.ids import sha256_hex

DATA = b"some object"
OID = sha256_hex(DATA)


def far():
    return time.monotonic() + 60


def test_put_writes_n_replicas_in_placement_order():
    c = FakeCluster()
    c.put(OID, DATA, deadline=far(), op_id="t")
    assert sorted(c.holders(OID)) == sorted(placement_order(OID, c.stores)[:3])


def test_put_skips_down_stores():
    c = FakeCluster()
    first = placement_order(OID, c.stores)[0]
    c.down(first)
    c.put(OID, DATA, deadline=far(), op_id="t")
    assert first not in c.holders(OID) and len(c.holders(OID)) == 3


def test_put_needs_n_alive_stores():
    c = FakeCluster()
    c.down("store-1", "store-2", "store-3")
    with pytest.raises(Unavailable):
        c.put(OID, DATA, deadline=far(), op_id="t")


def test_put_checks_the_hash():
    with pytest.raises(Invalid):
        FakeCluster().put("0" * 64, DATA, deadline=far(), op_id="t")


def test_interrupted_put_leaves_partial_replicas():
    c = FakeCluster()
    c.fail_put_after(OID, 1)
    with pytest.raises(Unavailable):
        c.put(OID, DATA, deadline=far(), op_id="t")
    assert len(c.holders(OID)) == 1
    c.put(OID, DATA, deadline=far(), op_id="t")  # the injected failure happens once
    assert len(c.holders(OID)) == 3


def test_put_everywhere_reaches_every_alive_store():
    c = FakeCluster()
    c.down("store-5")
    c.put_everywhere(OID, DATA, deadline=far(), op_id="t")
    assert sorted(c.holders(OID)) == ["store-1", "store-2", "store-3", "store-4"]


@pytest.mark.parametrize("down,expected", [(0, NotFound), (2, NotFound), (3, Unavailable)])
def test_get_of_a_missing_object(down, expected):
    c = FakeCluster()
    c.down(*c.stores[:down])
    with pytest.raises(expected):
        c.get(OID, deadline=far(), op_id="t")


def test_get_with_two_holders_down():
    c = FakeCluster()
    c.put(OID, DATA, deadline=far(), op_id="t")
    c.down(*c.holders(OID)[:2])
    assert c.get(OID, deadline=far(), op_id="t") == DATA


def test_scatter_reports_unreachable_stores():
    c = FakeCluster()
    c.down("store-2", "store-4")
    gathered = c.scatter("search", "", deadline=far(), op_id="t")
    assert gathered.unreachable == ("store-2", "store-4")
    assert sorted(gathered.responses) == ["store-1", "store-3", "store-5"]


def test_expired_deadline():
    c = FakeCluster()
    past = time.monotonic() - 1
    with pytest.raises(Unavailable):
        c.put(OID, DATA, deadline=past, op_id="t")
    assert c.scatter("search", "", deadline=past, op_id="t").unreachable == c.stores
