"""Identifiers and idempotency keys.

* ``uuid7`` — time-ordered UUIDs (RFC 9562) so B-tree inserts stay append-mostly.
* ``stable_hash`` — canonical-JSON hash used for activity keys (§5.2: run_id + product_id + stage + input
  hash) so a retried activity maps to the same key and returns the same result.
* ``sub_id_for`` — deterministic per-publication subId (§6: publication ↔ subId is 1:1), derived from
  the publication idempotency key so re-running link creation can never mint a second subId.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from typing import Any

import orjson

_last_ms = 0
_seq = 0


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7 with a 12-bit monotonic counter for same-millisecond calls."""
    global _last_ms, _seq
    ms = time.time_ns() // 1_000_000
    if ms <= _last_ms:
        ms = _last_ms
        _seq = (_seq + 1) & 0xFFF
        if _seq == 0:  # counter overflow inside one millisecond: borrow the next one
            ms += 1
    else:
        _seq = int.from_bytes(os.urandom(2), "big") & 0x3FF  # random start, room to count up
    _last_ms = ms
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= _seq << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def canonical_json(obj: Any) -> bytes:
    """Byte-stable JSON (sorted keys, no whitespace) — the only form ever hashed."""
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS, default=_default)


def _default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not canonically serialisable: {type(obj).__name__}")


def stable_hash(obj: Any, *, length: int = 32) -> str:
    return hashlib.blake2b(canonical_json(obj), digest_size=32).hexdigest()[:length]


def activity_key(run_id: str | uuid.UUID, product_id: int | str, stage: str, inputs: Any) -> str:
    """§5.2 idempotency key for one pipeline stage."""
    return f"{run_id}:{product_id}:{stage}:{stable_hash(inputs, length=16)}"


def publication_key(content_id: str | uuid.UUID, platform: str, account_id: str | uuid.UUID) -> str:
    """§4 M8 idempotency key = content_id + platform + account."""
    return f"{content_id}:{platform}:{account_id}"


_B32 = str.maketrans("", "", "=")


def sub_id_for(publication_idempotency_key: str, *, prefix: str = "a") -> str:
    """16-char lowercase alphanumeric subId (75 bits of entropy → collision-free at our scale).

    Birthday bound: with 10⁶ publications, P(collision) ≈ n²/2^76 ≈ 1.3·10⁻¹¹.
    """
    digest = hashlib.blake2b(publication_idempotency_key.encode(), digest_size=10).digest()
    body = base64.b32encode(digest).decode().translate(_B32).lower()
    return (prefix + body)[:16]


def short_code(seed: str, length: int = 8) -> str:
    """Short-URL code for the link-hub redirect (/r/{code})."""
    digest = hashlib.blake2b(seed.encode(), digest_size=8).digest()
    return base64.b32encode(digest).decode().translate(_B32).lower()[:length]
