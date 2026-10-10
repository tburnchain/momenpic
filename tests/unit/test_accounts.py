"""Member sign-up, approval, login sessions (HttpOnly cookie), lockout, CSRF and owner protection."""

from __future__ import annotations

import httpx
import pytest

from acvm.cli import seed_workspace
from acvm.core.enums import Role
from acvm.core.errors import PolicyViolation, ValidationFailed
from acvm.security.accounts import Accounts, AuthFailed
from acvm.security.passwords import hash_password, needs_rehash, password_problems, verify_password

OWNER_PW = "Owner-Test-9x!"  # test-only credentials
MEMBER_PW = "Member-Test-7y#"
CLIENT = {"X-ACVM-Client": "console"}


def test_password_hashing_and_policy() -> None:
    h = hash_password(OWNER_PW)
    assert h.startswith("scrypt$") and OWNER_PW not in h
    assert verify_password(OWNER_PW, h) and not verify_password("wrong", h) and not verify_password(OWNER_PW, None)
    assert hash_password(OWNER_PW) != h  # salted
    assert not needs_rehash(h) and needs_rehash(hash_password(OWNER_PW, n=2**10))
    assert password_problems(OWNER_PW, "boss@example.com") == []
    assert password_problems("short1A", "x@y.com")  # too short
    assert password_problems("alllowercaseletters", "x@y.com")  # one class only
    assert password_problems("Kimboss12!", "kimboss@x.com")  # contains the e-mail id


@pytest.fixture
async def api(ctx):  # type: ignore[no-untyped-def]
    from acvm.api.app import create_app

    ctx.settings.security.auth_mode = "session"
    await seed_workspace(ctx)
    app = create_app(ctx.settings, ctx=ctx)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acvm.test") as c):
        yield c, ctx


async def test_signup_approval_login_logout(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx = api
    assert (await c.get("/api/v1/auth/config")).json()["mode"] == "session"
    assert (await c.get("/api/v1/me")).status_code == 401
    # session mode: the dev token no longer works, and no password-less placeholder owner was seeded
    assert (await c.get("/api/v1/me", headers={"Authorization": f"Bearer {ctx.settings.security.dev_api_token}"})).status_code == 401
    assert [m.email for m in await Accounts(ctx).list()] == []

    owner = await Accounts(ctx).create("boss@example.com", OWNER_PW, role=Role.OWNER, name="대표")
    assert owner.status == "active" and owner.has_password

    r = await c.post("/api/v1/auth/signup", json={"email": "Staff@Example.com", "password": MEMBER_PW, "name": "직원"},
                     headers=CLIENT)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert (await c.post("/api/v1/auth/signup", json={"email": "staff@example.com", "password": MEMBER_PW},
                         headers=CLIENT)).status_code == 409
    weak = await c.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": "abcdefgh"}, headers=CLIENT)
    assert weak.status_code == 422
    pending = await c.post("/api/v1/auth/login", json={"email": "staff@example.com", "password": MEMBER_PW},
                           headers=CLIENT)
    assert pending.status_code == 401 and "승인 대기" in pending.json()["detail"]

    # owner logs in → HttpOnly cookie, /me is the member (no more dev@acvm.local)
    ok = await c.post("/api/v1/auth/login", json={"email": "BOSS@example.com", "password": OWNER_PW}, headers=CLIENT)
    assert ok.status_code == 200 and ok.json()["email"] == "boss@example.com" and ok.json()["role"] == "owner"
    cookie = ok.headers["set-cookie"]
    assert "acvm_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    me = (await c.get("/api/v1/me")).json()
    assert me["subject"] == "boss@example.com" and me["name"] == "대표" and me["auth"] == "session"

    # owner approves the new member
    users = (await c.get("/api/v1/users")).json()
    assert users["pending"] == 1
    staff = next(u for u in users["items"] if u["email"] == "staff@example.com")
    assert (await c.patch(f"/api/v1/users/{staff['id']}", json={"status": "active", "role": "operator"})).status_code == 403
    approved = await c.patch(f"/api/v1/users/{staff['id']}", json={"status": "active", "role": "operator"}, headers=CLIENT)
    assert approved.status_code == 200 and approved.json()["status"] == "active"

    # logout revokes the session server-side
    assert (await c.post("/api/v1/auth/logout", headers=CLIENT)).status_code == 200
    assert (await c.get("/api/v1/me")).status_code == 401

    async with httpx.AsyncClient(transport=c._transport, base_url="http://acvm.test") as staff_c:
        assert (await staff_c.post("/api/v1/auth/login", json={"email": "staff@example.com", "password": MEMBER_PW},
                                   headers=CLIENT)).status_code == 200
        assert (await staff_c.get("/api/v1/me")).json()["role"] == "operator"
        assert (await staff_c.get("/api/v1/users")).status_code == 403  # members admin is owner-only
    audit_actions = [a["action"] for a in (await _owner_client_audit(c))]
    assert {"auth.signup", "auth.login", "auth.logout", "member.update"} <= set(audit_actions)


async def _owner_client_audit(c: httpx.AsyncClient) -> list[dict[str, object]]:
    await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": OWNER_PW}, headers=CLIENT)
    body = (await c.get("/api/v1/audit", params={"limit": 200})).json()
    items = body["items"] if isinstance(body, dict) else body
    text = str(items)
    assert OWNER_PW not in text and MEMBER_PW not in text  # passwords never reach the audit log
    return list(items)


async def test_lockout_generic_errors_and_csrf(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx = api
    await Accounts(ctx).create("boss@example.com", OWNER_PW, role=Role.OWNER)
    unknown = await c.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": OWNER_PW},
                           headers=CLIENT)
    wrong = await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": "Wrong-Pass-1!"},
                         headers=CLIENT)
    assert unknown.status_code == wrong.status_code == 401 and unknown.json()["detail"] == wrong.json()["detail"]
    for _ in range(ctx.settings.security.login_max_failures - 1):
        await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": "Wrong-Pass-1!"},
                     headers=CLIENT)
    locked = await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": OWNER_PW},
                          headers=CLIENT)
    assert locked.status_code == 401 and "잠겼" in locked.json()["detail"]  # even the right password, while locked

    ctx.clock.advance(minutes=ctx.settings.security.login_lock_minutes + 1)  # type: ignore[attr-defined]
    assert (await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": OWNER_PW},
                         headers=CLIENT)).status_code == 200
    # cookie-authenticated writes need the console header and an allowed Origin
    assert (await c.post("/api/v1/auth/logout")).status_code == 403
    evil = await c.post("/api/v1/commands", json={"text": "주방 3개 글로"},
                        headers={**CLIENT, "Origin": "https://evil.example"})
    assert evil.status_code == 403


async def test_service_token_and_owner_protection(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx = api
    owner = await Accounts(ctx).create("boss@example.com", OWNER_PW, role=Role.OWNER)
    assert (await c.get("/api/v1/me", headers={"Authorization": "Bearer svc-test-token"})).status_code == 401
    ctx.settings.security.service_token = "svc-test-token"
    svc = (await c.get("/api/v1/me", headers={"Authorization": "Bearer svc-test-token"})).json()
    assert svc["subject"] == "service" and svc["auth"] == "service"

    accounts = Accounts(ctx)
    with pytest.raises(PolicyViolation):  # the last active owner stays an owner
        await accounts.update(owner.id, actor_id=None, role=Role.VIEWER)
    with pytest.raises(PolicyViolation):
        await accounts.update(owner.id, actor_id=owner.id, status="disabled")
    with pytest.raises(PolicyViolation):
        await accounts.delete(owner.id, actor_id=None)
    with pytest.raises(ValidationFailed):
        await accounts.create("weak@example.com", "password", role=Role.VIEWER)

    # password change keeps this session, signs out the others
    async with httpx.AsyncClient(transport=c._transport, base_url="http://acvm.test") as other:
        await other.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": OWNER_PW}, headers=CLIENT)
        await c.post("/api/v1/auth/login", json={"email": "boss@example.com", "password": OWNER_PW}, headers=CLIENT)
        changed = await c.post("/api/v1/auth/password", json={"current": OWNER_PW, "new": "Owner-New-8z!"},
                               headers=CLIENT)
        assert changed.status_code == 200
        assert (await c.get("/api/v1/me")).status_code == 200
        assert (await other.get("/api/v1/me")).status_code == 401
    with pytest.raises(AuthFailed):
        await accounts.login("boss@example.com", OWNER_PW, ip="1.2.3.4")


async def test_cli_owner_replaces_seed_placeholder(ctx) -> None:  # type: ignore[no-untyped-def]
    await seed_workspace(ctx)  # dev mode seeds the password-less owner@acvm.local placeholder
    accounts = Accounts(ctx)
    assert [m.email for m in await accounts.list()] == ["owner@acvm.local"]
    await accounts.create("boss@example.com", OWNER_PW, role=Role.OWNER, name="대표")
    assert [(m.email, m.role.value, m.status) for m in await accounts.list()] == [("boss@example.com", "owner", "active")]
    again = await accounts.create("boss@example.com", "Owner-Again-5q!", role=Role.OWNER)  # upsert = password reset
    assert again.has_password
    _member, token = await accounts.login("boss@example.com", "Owner-Again-5q!", ip="1.2.3.4")
    assert (await accounts.resolve(token)) is not None and (await accounts.resolve("forged")) is None
    await accounts.logout(token)
    assert await accounts.resolve(token) is None
