"""Error classes shared by the client library and the stores (principle P5).

Each class tells the caller what to do next, not just that something failed.
Errors cross process boundaries (a store raises them inside a Ray actor and
the client catches them in the driver), so every class must survive pickling
with its fields intact.
"""


class DtnError(Exception):
    """Base class. Carries a readable message and the operation id (P7)."""

    def __init__(self, message: str, *, op_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.op_id = op_id

    def _fields(self) -> dict:
        return {"op_id": self.op_id}

    def __reduce__(self):
        # The default reduce of Exception calls the class with self.args
        # (the message alone) and then copies the attributes on top: __init__
        # runs with default values, and a required keyword-only field would
        # make the rebuild fail on the receiving side. Rebuild through
        # __init__ with every field instead.
        return (_rebuild, (type(self), self.message, self._fields()))

    def __str__(self) -> str:
        if self.op_id is None:
            return self.message
        return f"{self.message} [op {self.op_id}]"


def _rebuild(cls, message, fields):
    return cls(message, **fields)


class Invalid(DtnError):
    """The request is malformed. Repeating it unchanged fails the same way."""


class NotFound(DtnError):
    """The object or track does not exist, and the answer is definitive.

    For a track, ``withdrawn`` tells apart "never existed" from "withdrawn".
    """

    def __init__(self, message: str, *, withdrawn: bool = False, op_id: str | None = None):
        super().__init__(message, op_id=op_id)
        self.withdrawn = withdrawn

    def _fields(self) -> dict:
        return {**super()._fields(), "withdrawn": self.withdrawn}


class Unavailable(DtnError):
    """Temporary failure: retry later or elsewhere.

    On writes the outcome is uncertain. ``saturated`` marks a store that
    rejected the request because it was at its concurrency limit: it is alive,
    so the failure detector must not count it as a failure.
    """

    def __init__(self, message: str, *, saturated: bool = False, op_id: str | None = None):
        super().__init__(message, op_id=op_id)
        self.saturated = saturated

    def _fields(self) -> dict:
        return {**super()._fields(), "saturated": self.saturated}


class Corrupt(DtnError):
    """A stored copy does not match its hash. Discard it and use another replica.

    Internal to the system: the client library never returns it to a user.
    """


class Conflict(DtnError):
    """The request contradicts the current state, e.g. uploading a withdrawn track."""


class NotImplemented(DtnError):  # noqa: A001 - name fixed by principle P5
    """The operation does not exist yet. Do not retry."""
