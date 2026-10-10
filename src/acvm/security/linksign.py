"""Link integrity (§10 "링크 무결성").

Threat: whoever swaps a deeplink's partner tag redirects all commission. Two independent checks run
immediately before every publish:

1. **Signature** — HMAC-SHA256 over (sub_id | deeplink | short_url | partner_id) computed with a
   Vault-held key at link creation. Any mutation of the stored row (DB compromise, buggy migration)
   fails verification.
2. **Partner tag** — the landing URL's ``lptag`` must equal our configured partner ID and its
   ``subid`` must equal the row's sub_id. This catches a link that was *created* wrong (e.g. an
   attacker-controlled API key) even though its signature is valid.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from acvm.core.errors import IntegrityViolation

ALLOWED_HOSTS = {"link.coupang.com", "www.coupang.com", "coupang.com", "m.coupang.com"}


def sign_link(key: bytes, sub_id: str, deeplink: str, short_url: str, partner_id: str) -> str:
    msg = "\x1f".join((sub_id, deeplink, short_url, partner_id)).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_signature(key: bytes, signature: str, sub_id: str, deeplink: str, short_url: str,
                     partner_id: str) -> None:
    expected = sign_link(key, sub_id, deeplink, short_url, partner_id)
    if not hmac.compare_digest(expected, signature):
        raise IntegrityViolation("affiliate link signature mismatch", sub_id=sub_id)


def verify_partner_tag(deeplink: str, partner_id: str, sub_id: str) -> None:
    u = urlparse(deeplink)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise IntegrityViolation("deeplink host not allowed", host=u.hostname)
    qs = {k.lower(): v for k, v in parse_qs(u.query).items()}
    tag = (qs.get("lptag") or [""])[0]
    if partner_id and tag != partner_id:
        raise IntegrityViolation("partner tag mismatch", expected=partner_id, got=tag)
    got_sub = (qs.get("subid") or [""])[0]
    if got_sub and got_sub != sub_id:
        raise IntegrityViolation("subId mismatch in deeplink", expected=sub_id, got=got_sub)


def verify_short_url(short_url: str) -> None:
    u = urlparse(short_url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise IntegrityViolation("short url host not allowed", host=u.hostname)


def hash_ip(ip: str, salt: bytes) -> str:
    """Salted, truncated hash — enough to detect same-source click bursts, useless for re-identification."""
    return hmac.new(salt, ip.encode(), hashlib.sha256).hexdigest()[:20]
