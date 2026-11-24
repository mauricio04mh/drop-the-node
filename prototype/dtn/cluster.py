"""The boundary between track operations and replicated storage.

The client library (upload, search, withdraw) only talks to a ``Cluster``. The
Ray implementation places replicas, detects failures, hedges reads and retries
saturated stores behind it; tests use an in-memory fake with the same
semantics. Neither side needs the other to make progress.

Conventions for every method:

- ``deadline`` is an absolute time on ``time.monotonic()`` in the calling
  process. When it passes, the call stops waiting and raises ``Unavailable``.
- ``op_id`` identifies the user operation and is logged by every store the
  call reaches (principle P7).
- A store that does not answer before the deadline, for whatever reason
  (crashed, partitioned, saturated, disk error), counts as unreachable.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Scattered:
    """Answers from asking every store the same question.

    ``responses`` maps each store's logical name to its return value;
    ``unreachable`` lists the stores that gave no usable answer. Every store
    appears in exactly one of the two.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    unreachable: tuple[str, ...] = ()


class Cluster(Protocol):
    stores: tuple[str, ...]
    """Logical names of every store (S of them), from static configuration."""

    replicas: int
    """N: how many stores keep each object."""

    def put(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        """Store an object on N stores, following its placement order.

        Down or suspected stores are skipped; a saturated store is retried
        with exponential backoff so the replica lands where readers look first.
        Returns once N stores confirmed the object durable. Raises ``Invalid``
        if ``data`` does not hash to ``obj_id``, and ``Unavailable`` if fewer
        than N confirmed before the deadline: the outcome is then uncertain and
        repeating the call is safe.
        """
        ...

    def put_everywhere(self, obj_id: str, data: bytes, *, deadline: float, op_id: str) -> None:
        """Store an object on every reachable store. Used for tombstones.

        Returns once N stores confirmed it, after waiting for the others up to
        the deadline; repair copies it later to the stores that missed it.
        Raises ``Unavailable`` (uncertain outcome) if fewer than N confirmed.
        """
        ...

    def get(self, obj_id: str, *, deadline: float, op_id: str) -> bytes:
        """Fetch an object from any replica, checking its hash.

        Asks stores in placement order, hedging slow ones and skipping
        corrupt copies. Raises ``NotFound`` only when no store has it and
        fewer than N stores were unreachable, so the absence is certain;
        otherwise ``Unavailable``.
        """
        ...

    def scatter(self, method: str, *args: Any, deadline: float, op_id: str) -> Scattered:
        """Call ``method`` of the store interface on every store in parallel.

        Used for ``search`` and ``tombstones``. Never raises for lack of
        stores: the caller decides from ``len(unreachable) < replicas``
        whether the gathered answer is complete.
        """
        ...
