"""Authentication & authorisation (§10 접근 제어, 네트워크).

Production: the console/API sit behind Cloudflare Access (SSO). Every request carries
``Cf-Access-Jwt-Assertion``; we verify its RS256 signature against the team JWKS, the audience tag and
the issuer, then map the email to an ``app_user`` row for the role. Unknown identities get 403 even
if Access let them through — Access proves *who*, ACVM decides *what*.

Session (single-host deployments): members log in with e-mail + password; the API sets an HttpOnly cookie whose
SHA-256 is looked up in ``auth_session`` (:mod:`acvm.security.accounts`). Cookie-authenticated unsafe requests must
carry the ``X-ACVM-Client`` header and, when the browser sends one, an allowed ``Origin`` (CSRF). An optional
``service_token`` lets scripts call the API as ``service``.

Dev/test: a static bearer token maps to the owner role (refused when env=prod); a session cookie also works so the
login flow can be exercised locally.
"""

from __future__ import annotations

import hmac
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from acvm.context import AppContext
from acvm.core.enums import Role
from acvm.db import tables as t
from acvm.security.rbac import Perm, Principal

_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def _jwks(team_domain: str) -> dict[str, Any]:
    hit = _JWKS_CACHE.get(team_domain)
    if hit and hit[0] > time.time():
        return hit[1]
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(f"https://{team_domain}/cdn-cgi/access/certs")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
    _JWKS_CACHE[team_domain] = (time.time() + 3600, data)
    return data


async def verify_access_jwt(token: str, team_domain: str, audience: str) -> dict[str, Any]:
    jwks = await _jwks(team_domain)
    header = jwt.get_unverified_header(token)
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown signing key")
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
    try:
        claims: dict[str, Any] = jwt.decode(token, public_key, algorithms=["RS256"],  # type: ignore[arg-type]
                                            audience=audience, issuer=f"https://{team_domain}")
        return claims
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid access token: {exc}") from exc


def ctx_of(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CLIENT_HEADER = "X-ACVM-Client"


def csrf_ok(request: Request) -> bool:
    """Cookie-authenticated writes must come from the console: custom header (not settable cross-site without a
    CORS preflight) and, if present, an Origin that is this host or an allowed console origin."""
    if request.method in SAFE_METHODS:
        return True
    if not request.headers.get(CLIENT_HEADER):
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    allowed = {o.rstrip("/") for o in ctx_of(request).settings.console_origins}
    return urlsplit(origin).netloc == host or origin.rstrip("/") in allowed


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "") or "")[:64]


async def session_principal(request: Request) -> Principal | None:
    ctx = ctx_of(request)
    token = request.cookies.get(ctx.settings.security.session_cookie)
    if not token:
        return None
    from acvm.security.accounts import Accounts

    member = await Accounts(ctx).resolve(token)
    if member is None:
        return None
    if not csrf_ok(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
    return Principal(subject=member.email, role=member.role, workspace_id=str(member.workspace_id),
                     user_id=str(member.id), name=member.name, auth="session")


async def get_principal(request: Request) -> Principal:
    ctx = ctx_of(request)
    sec = ctx.settings.security
    if sec.auth_mode == "cloudflare_access":
        token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Cloudflare Access token")
        claims = await verify_access_jwt(token, sec.cf_access_team_domain, sec.cf_access_audience)
        email = str(claims.get("email", "")).lower()
        async with ctx.uow() as repos:
            row = (await repos.session.execute(select(t.AppUserRow).where(t.AppUserRow.email == email))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user not provisioned in ACVM")
        return Principal(subject=email, role=Role(row.role), workspace_id=str(row.workspace_id),
                         user_id=str(row.id), name=row.display_name or "", auth="cloudflare_access")
    member = await session_principal(request)
    if member is not None:
        return member
    if sec.auth_mode == "session":
        auth = request.headers.get("Authorization", "")
        if sec.service_token and hmac.compare_digest(auth.encode(), f"Bearer {sec.service_token}".encode()):
            async with ctx.uow() as repos:
                ws = await repos.workspaces.first()
            return Principal(subject="service", role=Role.OWNER, workspace_id=str(ws.id) if ws else None,
                             auth="service")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다")
    if ctx.settings.env == "prod":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "dev auth disabled in prod")
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {sec.dev_api_token}".encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    role = Role(request.headers.get("X-Dev-Role", "owner")) if ctx.settings.env in ("dev", "test") else Role.OWNER
    async with ctx.uow() as repos:
        ws = await repos.workspaces.first()
    return Principal(subject=request.headers.get("X-Dev-User", "dev@acvm.local"), role=role,
                     workspace_id=str(ws.id) if ws else None)


def require(perm: Perm):  # type: ignore[no-untyped-def]
    async def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not p.can(perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {p.role.value} lacks {perm.value}")
        if p.workspace_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "no workspace provisioned — run `acvm seed`")
        return p

    return dep
