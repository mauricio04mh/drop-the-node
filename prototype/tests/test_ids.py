import pytest

from dtn.errors import Invalid
from dtn.ids import is_valid_id, require_id, sha256_hex

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_hex_matches_the_standard_digest():
    assert sha256_hex(b"") == EMPTY_SHA256


@pytest.mark.parametrize(
    "value",
    [
        EMPTY_SHA256.upper(),  # uppercase is not canonical
        EMPTY_SHA256[:-1],  # too short
        EMPTY_SHA256 + "0",  # too long
        "g" * 64,  # not hex
        None,
        b"\x00" * 32,
    ],
)
def test_rejects_values_that_are_not_identifiers(value):
    assert not is_valid_id(value)
    with pytest.raises(Invalid):
        require_id(value)


def test_accepts_a_lowercase_sha256():
    assert require_id(EMPTY_SHA256) == EMPTY_SHA256
