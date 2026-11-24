"""Errors keep their class and fields when they cross a process boundary."""

import pickle

import pytest

from dtn.errors import (
    Conflict,
    Corrupt,
    DtnError,
    Invalid,
    NotFound,
    NotImplemented,
    Unavailable,
)


@pytest.mark.parametrize("cls", [Invalid, Corrupt, Conflict, NotImplemented])
def test_plain_errors_survive_pickling(cls):
    error = cls("boom", op_id="op-1")
    copy = pickle.loads(pickle.dumps(error))
    assert type(copy) is cls
    assert copy.message == "boom"
    assert copy.op_id == "op-1"


def test_not_found_keeps_withdrawn_flag():
    copy = pickle.loads(pickle.dumps(NotFound("gone", withdrawn=True, op_id="op-2")))
    assert copy.withdrawn is True
    assert copy.op_id == "op-2"


def test_unavailable_keeps_saturated_flag():
    copy = pickle.loads(pickle.dumps(Unavailable("busy", saturated=True)))
    assert copy.saturated is True
    assert copy.op_id is None


def test_every_error_is_a_dtn_error():
    for cls in (Invalid, NotFound, Unavailable, Corrupt, Conflict, NotImplemented):
        assert issubclass(cls, DtnError)


def test_message_mentions_the_operation():
    assert str(Invalid("bad id", op_id="op-3")) == "bad id [op op-3]"
    assert str(Invalid("bad id")) == "bad id"
