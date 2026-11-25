"""Where each object lives: rendezvous (highest random weight) hashing.

Every store gets a score for every object, ``SHA-256(store name ‖ object id)``,
and the object's placement order is the stores sorted by score, highest
first. Writes go to the first N live stores of that order; reads walk it.

Properties the design relies on:

- every client computes the same order from the same configuration, with no
  table of locations to keep in sync;
- the order uses logical names, so a restarted store keeps its place;
- removing a store does not reorder the others: each object only loses that
  one entry, so only its replicas there need to move.
"""

import hashlib
from collections.abc import Iterable

# Separates name and id so that no two (name, id) pairs hash the same bytes.
_SEPARATOR = b"\x00"


def score(store: str, obj_id: str) -> bytes:
    return hashlib.sha256(store.encode() + _SEPARATOR + obj_id.encode()).digest()


def placement_order(obj_id: str, stores: Iterable[str]) -> tuple[str, ...]:
    """All ``stores``, from the most to the least preferred for ``obj_id``."""
    return tuple(sorted(stores, key=lambda s: score(s, obj_id), reverse=True))
