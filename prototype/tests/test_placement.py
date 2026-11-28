from collections import Counter

from dtn.ids import sha256_hex
from dtn.placement import placement_order

STORES = ("store-1", "store-2", "store-3", "store-4", "store-5")


def ids(count):
    return [sha256_hex(str(i).encode()) for i in range(count)]


def test_order_is_a_deterministic_permutation_of_all_stores():
    obj_id = sha256_hex(b"x")
    order = placement_order(obj_id, STORES)
    assert sorted(order) == sorted(STORES)
    assert placement_order(obj_id, reversed(STORES)) == order


def test_objects_spread_evenly_over_the_stores():
    firsts = Counter(placement_order(i, STORES)[0] for i in ids(5000))
    for store in STORES:
        assert 850 < firsts[store] < 1150  # 1000 expected for each


def test_removing_a_store_does_not_reorder_the_others():
    """Only objects that had a replica on the removed store need to move."""
    remaining = STORES[:-1]
    for obj_id in ids(500):
        full = placement_order(obj_id, STORES)
        assert placement_order(obj_id, remaining) == tuple(s for s in full if s != "store-5")
