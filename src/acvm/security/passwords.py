"""Password hashing (stdlib scrypt, PHC-style string) and the console password policy.

Stored format: ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>`` — parameters travel with the hash so they can be raised
later (:func:`needs_rehash`). Verification is constant-time. The plaintext is never logged or stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

N, R, P, DKLEN = 2**15, 8, 1, 32
_MAXMEM = 128 * 1024 * 1024
MIN_LENGTH = 8
MAX_LENGTH = 128


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, n: int = N, r: int = R, p: int = P) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=_MAXMEM)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algo, n, r, p, salt, want = encoded.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(_unb64(want)), maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, _unb64(want))


def needs_rehash(encoded: str | None) -> bool:
    try:
        algo, n, r, p, *_ = (encoded or "").split("$")
        return algo != "scrypt" or (int(n), int(r), int(p)) < (N, R, P)
    except ValueError:
        return True


# A precomputed hash used to spend the same time when an e-mail does not exist (no user-enumeration by timing).
_DUMMY = hash_password(secrets.token_urlsafe(16))


def burn_time(password: str) -> None:
    verify_password(password, _DUMMY)


def password_problems(password: str, email: str = "") -> list[str]:
    """Korean messages for a password that does not meet the policy (empty list = acceptable)."""
    out: list[str] = []
    if len(password) < MIN_LENGTH:
        out.append(f"비밀번호는 {MIN_LENGTH}자 이상이어야 합니다")
    if len(password) > MAX_LENGTH:
        out.append(f"비밀번호는 {MAX_LENGTH}자 이하여야 합니다")
    classes = sum(bool(re.search(rx, password)) for rx in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        out.append("영문 대문자·소문자·숫자·특수문자 중 3종류 이상을 섞어 주세요")
    local = email.split("@", 1)[0].lower()
    if len(local) >= 4 and local in password.lower():
        out.append("비밀번호에 이메일 아이디를 넣지 마세요")
    if password.strip() != password:
        out.append("비밀번호 앞뒤에 공백을 넣지 마세요")
    return out


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email)) and len(email) <= 320
