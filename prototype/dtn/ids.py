"""Content identifiers (principle P2): every object is named by its SHA-256."""

import hashlib
import re

from dtn.errors import Invalid

_ID_PATTERN = re.compile(r"[0-9a-f]{64}")


def sha256_hex(data: bytes) -> str:
    """Identifier of an object: SHA-256 of its bytes, as 64 lowercase hex chars."""
    return hashlib.sha256(data).hexdigest()


def is_valid_id(value: object) -> bool:
    """True if ``value`` has the shape of an identifier."""
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def require_id(value: object, *, op_id: str | None = None) -> str:
    """Return ``value`` if it is a valid identifier; raise ``Invalid`` otherwise."""
    if not is_valid_id(value):
        raise Invalid(f"not a SHA-256 identifier: {value!r}", op_id=op_id)
    return value
