"""Members & login (auth_mode ``session``; also usable in dev).

    GET    /auth/config        public — login mode, sign-up policy, password rule
    POST   /auth/signup        public — new member (pending until an owner approves, see ``signup_mode``)
    POST   /auth/login         public — e-mail + password → HttpOnly session cookie
    POST   /auth/logout        revoke the current session
    POST   /auth/password      change own password (other sessions are signed out)
    GET    /users              members (owner)            POST /users  create member with an initial password
    PATCH  /users/{id}         approve / role / disable    DELETE /users/{id}

Passwords and session tokens are never logged or audited; the audit log records who did what.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from acvm.api.auth import CLIENT_HEADER, client_ip, csrf_ok, ctx_of, get_principal, require
from acvm.core.enums import Role
from acvm.core.errors import ACVMError, Conflict, NotFound, PolicyViolation, ValidationFailed
from acvm.security.accounts import Accounts, AuthFailed
from acvm.security.audit import audit
from acvm.security.passwords import MIN_LENGTH
from acvm.security.rbac import Perm, Principal

router = APIRouter(prefix="/api/v1")


def _err(exc: ACVMError) -> HTTPException:
    code = 401 if isinstance(exc, AuthFailed) else 404 if isinstance(exc, NotFound) else \
        409 if isinstance(exc, Conflict) else 403 if isinstance(exc, PolicyViolation) else 422
    return HTTPException(code, str(exc))


def _require_client(request: Request) -> None:
    if not csrf_ok(request):
        raise HTTPException(403, f"console requests must send {CLIENT_HEADER}")


def _set_cookie(request: Request, response: Response, token: str) -> None:
    sec = ctx_of(request).settings.security
    response.set_cookie(sec.session_cookie, token, max_age=sec.session_ttl_hours * 3600, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class SignupIn(LoginIn):
    name: str = Field(default="", max_length=80)


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class MemberCreate(SignupIn):
    role: Role = Role.VIEWER


class MemberPatch(BaseModel):
    role: Role | None = None
    status: Literal["pending", "active", "disabled"] | None = None
    name: str | None = Field(default=None, max_length=80)


@router.get("/auth/config")
async def auth_config(request: Request) -> dict[str, Any]:
    sec = ctx_of(request).settings.security
    return {"mode": sec.auth_mode, "signup": sec.signup_mode, "password_min": MIN_LENGTH,
            "password_rule": f"{MIN_LENGTH}자 이상, 영문 대문자·소문자·숫자·특수문자 중 3종류 이상"}


@router.post("/auth/signup")
async def signup(body: SignupIn, request: Request) -> dict[str, Any]:
    _require_client(request)
    ctx = ctx_of(request)
    try:
        member = await Accounts(ctx).signup(body.email, body.password, body.name)
    except ACVMError as exc:
        raise _err(exc) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, member.email, "auth.signup", str(member.id), status=member.status,
                    ip=client_ip(request))
    msg = ("가입이 완료되었습니다. 로그인하세요." if member.status == "active"
           else "가입 신청이 접수되었습니다. 소유자가 승인하면 로그인할 수 있습니다.")
    return {"status": member.status, "message": msg, "email": member.email}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    _require_client(request)
    ctx = ctx_of(request)
    ip = client_ip(request)
    try:
        member, token = await Accounts(ctx).login(body.email, body.password, ip=ip,
                                                  user_agent=request.headers.get("user-agent", ""))
    except AuthFailed as exc:
        async with ctx.uow() as repos:
            await audit(repos, ctx.clock, body.email.strip().lower()[:320], "auth.login_failed", "-", ip=ip)
        raise _err(exc) from exc
    _set_cookie(request, response, token)
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, member.email, "auth.login", str(member.id), ip=ip)
    return {"subject": member.email, "email": member.email, "name": member.name, "role": member.role.value,
            "workspace_id": str(member.workspace_id), "user_id": str(member.id), "auth": "session"}


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    _require_client(request)
    ctx = ctx_of(request)
    sec = ctx.settings.security
    token = request.cookies.get(sec.session_cookie)
    who = await Accounts(ctx).resolve(token)
    await Accounts(ctx).logout(token)
    response.delete_cookie(sec.session_cookie, path="/")
    if who is not None:
        async with ctx.uow() as repos:
            await audit(repos, ctx.clock, who.email, "auth.logout", str(who.id))
    return {"ok": True}


@router.post("/auth/password")
async def change_password(body: PasswordIn, request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    if not p.user_id:
        raise HTTPException(400, "회원 로그인으로만 비밀번호를 바꿀 수 있습니다")
    ctx = ctx_of(request)
    try:
        await Accounts(ctx).change_password(uuid.UUID(p.user_id), body.current, body.new,
                                            keep_session=request.cookies.get(ctx.settings.security.session_cookie))
    except ACVMError as exc:
        raise _err(exc) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "auth.password_changed", p.user_id)
    return {"ok": True}


def _actor(p: Principal) -> uuid.UUID | None:
    return uuid.UUID(p.user_id) if p.user_id else None


@router.get("/users")
async def list_users(request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    members = await Accounts(ctx_of(request)).list()
    return {"items": [m.as_dict() for m in members],
            "pending": sum(1 for m in members if m.status == "pending")}


@router.post("/users")
async def create_user(body: MemberCreate, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    ctx = ctx_of(request)
    accounts = Accounts(ctx)
    if any(m.email == body.email.strip().lower() for m in await accounts.list()):
        raise HTTPException(409, "이미 가입된 이메일입니다")
    try:
        member = await accounts.create(body.email, body.password, role=body.role, name=body.name)
    except ValidationFailed as exc:
        raise _err(exc) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "member.create", str(member.id), email=member.email,
                    role=member.role.value)
    return member.as_dict()


@router.patch("/users/{user_id}")
async def update_user(user_id: uuid.UUID, body: MemberPatch, request: Request,
                      p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    ctx = ctx_of(request)
    try:
        member = await Accounts(ctx).update(user_id, actor_id=_actor(p), role=body.role, status=body.status,
                                            name=body.name)
    except ACVMError as exc:
        raise _err(exc) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "member.update", str(user_id),
                    **body.model_dump(exclude_none=True, mode="json"))
    return member.as_dict()


@router.delete("/users/{user_id}")
async def delete_user(user_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    ctx = ctx_of(request)
    try:
        await Accounts(ctx).delete(user_id, actor_id=_actor(p))
    except ACVMError as exc:
        raise _err(exc) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "member.delete", str(user_id))
    return {"ok": True}
