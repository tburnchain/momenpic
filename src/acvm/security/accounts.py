"""Console members: sign-up, login sessions, approval and roles (auth_mode ``session``; sessions also work in dev).

* **Sign-up** (``signup_mode``): ``approval`` (default) — the member starts ``pending`` as a viewer and an owner
  activates them in 회원 관리; ``open`` — active viewer at once; ``closed`` — owners create members.
* **Login** — email + password (scrypt). Failed attempts lock the account for ``login_lock_minutes`` after
  ``login_max_failures``; logins are also limited per IP. The answer never reveals whether an e-mail exists.
* **Sessions** — a random 256-bit token in an HttpOnly cookie; only its SHA-256 is stored (``auth_session``).
  Logout revokes it; a password change revokes every other session of that member.
* **Owners** — the first owner is created on the server with ``acvm user create … --role owner`` (password typed at
  a hidden prompt — never in code, files or logs). The last active owner cannot be demoted, disabled or removed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update

from acvm.core.enums import Role
from acvm.core.errors import ACVMError, Conflict, NotFound, PolicyViolation, ValidationFailed
from acvm.core.ids import uuid7
from acvm.core.logging import get_logger
from acvm.db import tables as t
from acvm.security.passwords import (
    burn_time,
    hash_password,
    needs_rehash,
    normalize_email,
    password_problems,
    valid_email,
    verify_password,
)

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

STATUSES = ("pending", "active", "disabled")
PLACEHOLDER_OWNER = "owner@acvm.local"  # created by `acvm seed` for Cloudflare Access provisioning (no password)
_TOUCH_EVERY = timedelta(minutes=5)


class AuthFailed(ACVMError):
    """Generic login failure — the message never tells which part was wrong."""

    code = "auth_failed"


@dataclass(frozen=True, slots=True)
class Member:
    id: uuid.UUID
    email: str
    name: str
    role: Role
    status: str
    workspace_id: uuid.UUID
    created_at: datetime
    last_login_at: datetime | None
    has_password: bool

    @classmethod
    def of(cls, row: t.AppUserRow) -> Member:
        return cls(id=row.id, email=row.email, name=row.display_name or "", role=Role(row.role),
                   status=row.status or "active", workspace_id=row.workspace_id, created_at=row.created_at,
                   last_login_at=row.last_login_at, has_password=bool(row.password_hash))

    def as_dict(self) -> dict[str, Any]:
        return {"id": str(self.id), "email": self.email, "name": self.name, "role": self.role.value,
                "status": self.status, "workspace_id": str(self.workspace_id), "created_at": self.created_at.isoformat(),
                "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
                "has_password": self.has_password}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.sec = ctx.settings.security

    def _now(self) -> datetime:
        return self.ctx.clock.now()

    async def _by_email(self, repos: Any, email: str) -> t.AppUserRow | None:
        row: t.AppUserRow | None = (await repos.session.execute(
            select(t.AppUserRow).where(t.AppUserRow.email == normalize_email(email)))).scalar_one_or_none()
        return row

    async def _workspace_id(self, repos: Any) -> uuid.UUID:
        ws = await repos.workspaces.first()
        if ws is None:
            raise Conflict("no workspace provisioned — run `acvm seed`")
        wid: uuid.UUID = ws.id
        return wid

    @staticmethod
    def _check_credentials(email: str, password: str) -> None:
        if not valid_email(email):
            raise ValidationFailed("이메일 형식이 올바르지 않습니다")
        problems = password_problems(password, email)
        if problems:
            raise ValidationFailed(" · ".join(problems))

    # ── members ──

    async def signup(self, email: str, password: str, name: str = "") -> Member:
        mode = self.sec.signup_mode
        if mode == "closed":
            raise PolicyViolation("회원가입이 닫혀 있습니다 — 관리자에게 계정 생성을 요청하세요")
        email = normalize_email(email)
        self._check_credentials(email, password)
        async with self.ctx.uow() as repos:
            if await self._by_email(repos, email) is not None:
                raise Conflict("이미 가입된 이메일입니다")
            row = t.AppUserRow(id=uuid7(), workspace_id=await self._workspace_id(repos), email=email,
                               display_name=name.strip()[:80], role=Role.VIEWER.value,
                               status="active" if mode == "open" else "pending",
                               password_hash=hash_password(password), failed_logins=0, created_at=self._now(),
                               updated_at=self._now())
            repos.session.add(row)
            await repos.session.flush()
            member = Member.of(row)
        log.info("member_signup", email=email, status=member.status)
        return member

    async def create(self, email: str, password: str, *, role: Role, name: str = "", status: str = "active",
                     replace_password: bool = True) -> Member:
        """Owner/CLI path: create a member, or update an existing one (role, status, password)."""
        email = normalize_email(email)
        self._check_credentials(email, password)
        if status not in STATUSES:
            raise ValidationFailed("unknown status", status=status)
        async with self.ctx.uow() as repos:
            row = await self._by_email(repos, email)
            now = self._now()
            if row is None:
                row = t.AppUserRow(id=uuid7(), workspace_id=await self._workspace_id(repos), email=email,
                                   display_name=name.strip()[:80], role=role.value, status=status,
                                   password_hash=hash_password(password), failed_logins=0, created_at=now,
                                   updated_at=now)
                repos.session.add(row)
            else:
                row.role, row.status, row.updated_at = role.value, status, now
                if name.strip():
                    row.display_name = name.strip()[:80]
                if replace_password:
                    row.password_hash = hash_password(password)
                    row.failed_logins, row.locked_until = 0, None
                    await self._revoke_all(repos, row.id)
            if role is Role.OWNER and status == "active":  # the seed's password-less placeholder is no longer needed
                placeholder = await self._by_email(repos, PLACEHOLDER_OWNER)
                if placeholder is not None and placeholder.id != row.id and not placeholder.password_hash:
                    await repos.session.delete(placeholder)
            await repos.session.flush()
            return Member.of(row)

    async def list(self) -> list[Member]:
        async with self.ctx.uow() as repos:
            rows = (await repos.session.execute(select(t.AppUserRow).order_by(t.AppUserRow.created_at))).scalars().all()
            return [Member.of(r) for r in rows]

    async def get(self, user_id: uuid.UUID) -> Member:
        async with self.ctx.uow() as repos:
            row = await repos.session.get(t.AppUserRow, user_id)
            if row is None:
                raise NotFound("member not found")
            return Member.of(row)

    async def _active_owners(self, repos: Any, *, excluding: uuid.UUID | None = None) -> int:
        q = select(func.count()).select_from(t.AppUserRow).where(
            t.AppUserRow.role == Role.OWNER.value, func.coalesce(t.AppUserRow.status, "active") == "active",
            t.AppUserRow.password_hash.is_not(None))
        if excluding is not None:
            q = q.where(t.AppUserRow.id != excluding)
        return int((await repos.session.execute(q)).scalar_one())

    async def update(self, user_id: uuid.UUID, *, actor_id: uuid.UUID | None, role: Role | None = None,
                     status: str | None = None, name: str | None = None) -> Member:
        if status is not None and status not in STATUSES:
            raise ValidationFailed("unknown status", status=status)
        async with self.ctx.uow() as repos:
            row = await repos.session.get(t.AppUserRow, user_id)
            if row is None:
                raise NotFound("member not found")
            if actor_id == row.id and ((role is not None and role.value != row.role) or status not in (None, "active")):
                raise PolicyViolation("자기 자신의 역할·상태는 바꿀 수 없습니다")
            demoting = row.role == Role.OWNER.value and (
                (role is not None and role is not Role.OWNER) or status in ("disabled", "pending"))
            if demoting and await self._active_owners(repos, excluding=row.id) == 0:
                raise PolicyViolation("마지막 소유자는 강등·정지할 수 없습니다")
            if role is not None:
                row.role = role.value
            if status is not None:
                row.status = status
                if status != "active":
                    await self._revoke_all(repos, row.id)
                else:
                    row.failed_logins, row.locked_until = 0, None
            if name is not None:
                row.display_name = name.strip()[:80]
            row.updated_at = self._now()
            await repos.session.flush()
            return Member.of(row)

    async def delete(self, user_id: uuid.UUID, *, actor_id: uuid.UUID | None) -> None:
        async with self.ctx.uow() as repos:
            row = await repos.session.get(t.AppUserRow, user_id)
            if row is None:
                raise NotFound("member not found")
            if actor_id == row.id:
                raise PolicyViolation("자기 자신은 삭제할 수 없습니다")
            if row.role == Role.OWNER.value and await self._active_owners(repos, excluding=row.id) == 0:
                raise PolicyViolation("마지막 소유자는 삭제할 수 없습니다")
            await self._revoke_all(repos, row.id)
            await repos.session.delete(row)

    # ── sessions ──

    async def _ip_allowed(self, ip: str) -> bool:
        decision = await self.ctx.quota.acquire_window(f"login:ip:{ip}", self.sec.login_ip_limit_per_10min, 600,
                                                       self._now(), uuid.uuid4().hex)
        return decision.allowed

    async def login(self, email: str, password: str, *, ip: str = "", user_agent: str = "") -> tuple[Member, str]:
        """→ (member, raw session token). Raises :class:`AuthFailed` with a generic message."""
        email = normalize_email(email)
        if not await self._ip_allowed(ip or "unknown"):
            raise AuthFailed("로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요")
        now = self._now()
        failure: str | None = None
        member: Member | None = None
        token = ""
        async with self.ctx.uow() as repos:  # failures are committed too (lockout counter) — raise after the block
            row = await self._by_email(repos, email)
            if row is None or not row.password_hash:
                burn_time(password)
                failure = "이메일 또는 비밀번호가 올바르지 않습니다"
            elif row.locked_until is not None and row.locked_until > now:
                burn_time(password)
                failure = "로그인 실패가 반복되어 잠시 잠겼습니다. 잠시 후 다시 시도하세요"
            elif not verify_password(password, row.password_hash):
                row.failed_logins = (row.failed_logins or 0) + 1
                if row.failed_logins >= self.sec.login_max_failures:
                    row.locked_until = now + timedelta(minutes=self.sec.login_lock_minutes)
                    row.failed_logins = 0
                failure = "이메일 또는 비밀번호가 올바르지 않습니다"
            elif (row.status or "active") == "pending":
                failure = "가입 승인 대기 중입니다. 소유자가 승인하면 로그인할 수 있습니다"
            elif (row.status or "active") != "active":
                failure = "정지된 계정입니다. 소유자에게 문의하세요"
            else:
                if needs_rehash(row.password_hash):
                    row.password_hash = hash_password(password)
                row.failed_logins, row.locked_until, row.last_login_at = 0, None, now
                token = secrets.token_urlsafe(32)
                repos.session.add(t.AuthSessionRow(
                    id=uuid7(), user_id=row.id, token_hash=token_hash(token), created_at=now,
                    expires_at=now + timedelta(hours=self.sec.session_ttl_hours), last_seen_at=now,
                    ip=(ip or "")[:64], user_agent=(user_agent or "")[:400]))
                await repos.session.flush()
                member = Member.of(row)
        if failure is not None or member is None:
            log.info("login_failed", email=email, ip=ip)
            raise AuthFailed(failure or "이메일 또는 비밀번호가 올바르지 않습니다")
        log.info("login_ok", email=email, ip=ip)
        return member, token

    async def resolve(self, token: str | None) -> Member | None:
        """Member behind a session cookie (active, unexpired, not revoked) — else None."""
        if not token or len(token) > 200:
            return None
        now = self._now()
        async with self.ctx.uow() as repos:
            sess = (await repos.session.execute(select(t.AuthSessionRow).where(
                t.AuthSessionRow.token_hash == token_hash(token)))).scalar_one_or_none()
            if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
                return None
            row = await repos.session.get(t.AppUserRow, sess.user_id)
            if row is None or (row.status or "active") != "active" or not row.password_hash:
                return None
            if now - sess.last_seen_at > _TOUCH_EVERY:
                sess.last_seen_at = now
            return Member.of(row)

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        async with self.ctx.uow() as repos:
            await repos.session.execute(update(t.AuthSessionRow).where(
                t.AuthSessionRow.token_hash == token_hash(token), t.AuthSessionRow.revoked_at.is_(None)
            ).values(revoked_at=self._now()))

    async def _revoke_all(self, repos: Any, user_id: uuid.UUID, *, keep: str | None = None) -> None:
        q = update(t.AuthSessionRow).where(t.AuthSessionRow.user_id == user_id, t.AuthSessionRow.revoked_at.is_(None))
        if keep:
            q = q.where(t.AuthSessionRow.token_hash != token_hash(keep))
        await repos.session.execute(q.values(revoked_at=self._now()))

    async def change_password(self, user_id: uuid.UUID, current: str, new: str, *, keep_session: str | None) -> None:
        async with self.ctx.uow() as repos:
            row = await repos.session.get(t.AppUserRow, user_id)
            if row is None:
                raise NotFound("member not found")
            if not verify_password(current, row.password_hash):
                raise AuthFailed("현재 비밀번호가 올바르지 않습니다")
            problems = password_problems(new, row.email)
            if problems:
                raise ValidationFailed(" · ".join(problems))
            row.password_hash, row.updated_at = hash_password(new), self._now()
            await self._revoke_all(repos, row.id, keep=keep_session)

    async def purge_expired(self, *, older_than: timedelta = timedelta(days=30)) -> int:
        cutoff = self._now() - older_than
        async with self.ctx.uow() as repos:
            res = await repos.session.execute(delete(t.AuthSessionRow).where(t.AuthSessionRow.expires_at < cutoff))
            return int(getattr(res, "rowcount", 0) or 0)


__all__ = ["PLACEHOLDER_OWNER", "Accounts", "AuthFailed", "Member", "token_hash"]
