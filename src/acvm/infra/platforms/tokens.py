"""Credentials and the authenticated HTTP transport shared by every official-API platform client.

§10 (보안): social-account OAuth tokens are the most valuable asset in the system. They live only in the
secret store — ``ChannelAccount.token_ref`` is a path, never the token — and they never reach a log line:

* ``ApiTransport`` keeps tokens out of exception messages, and :func:`install_log_redaction` masks
  ``access_token`` / ``client_secret`` / ``refresh_token`` query parameters in httpx's own request log
  (Meta's documented flows pass tokens as query parameters).
* ``TokenBundle.__repr__`` never prints secret material.

Token lifecycle (§10 "토큰 수명"): Meta/Threads long-lived tokens are refreshed 7 days before expiry; Google
and TikTok access tokens are refreshed when (about to be) expired using the stored refresh token. A refresh
that fails is surfaced as :class:`TokenRefreshFailed`, which the distribution service turns into a 48-hour
account cooldown plus an alert. The refreshed material is written back with ``ctx.secrets.put``.

Error mapping shared by all clients (§9.5 #1, §7.1 #3):

* HTTP 429 / 5xx / 408 / network failures → ``UpstreamError(retryable=True)`` (Temporal retries);
* other HTTP 4xx                            → ``UpstreamError(retryable=False)`` (fail fast, no wasted quota);
* spam / policy / restriction signals       → :class:`PlatformPolicyWarning` (a ``PlatformWarning``), which
  triggers the immediate 48-hour cooldown and cancellation of the account's schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from acvm.core.clock import ensure_utc
from acvm.core.enums import Platform
from acvm.core.errors import ACVMError, UpstreamError
from acvm.core.logging import get_logger
from acvm.infra.platforms.base import PlatformClient, PlatformWarning

if TYPE_CHECKING:
    from acvm.context import AppContext
    from acvm.domain.models import ChannelAccount

log = get_logger(__name__)

# ───────────────────────────── log redaction ─────────────────────────────

SENSITIVE_PARAMS = frozenset({
    "access_token", "client_secret", "fb_exchange_token", "refresh_token", "input_token", "code", "client_key",
})


def redact_url(url: str | httpx.URL) -> str:
    """Return ``url`` with every sensitive query parameter value replaced by ``***``."""
    text = str(url)
    parts = urlsplit(text)
    if not parts.query:
        return text
    pairs = [(k, "***" if k.lower() in SENSITIVE_PARAMS else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(pairs, safe="*,")))


def url_path(url: str | httpx.URL) -> str:
    """Host + path only — safe to put into error messages."""
    parts = urlsplit(str(url))
    return f"{parts.netloc}{parts.path}"


class _HttpxRedactionFilter(logging.Filter):
    """Masks token-bearing query parameters in httpx's ``HTTP Request: METHOD URL`` INFO line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact_url(a) if isinstance(a, httpx.URL) or (isinstance(a, str) and "://" in a)
                                else a for a in record.args)
        return True


_redaction_installed = False


def install_log_redaction() -> None:
    """Idempotently attach the redaction filter to the ``httpx`` logger."""
    global _redaction_installed
    if not _redaction_installed:
        logging.getLogger("httpx").addFilter(_HttpxRedactionFilter())
        _redaction_installed = True


# ───────────────────────────── error mapping ─────────────────────────────


class PlatformPolicyWarning(PlatformWarning):
    """A spam / policy / restriction signal from a platform (§7.1 #3 → immediate cooldown, §9.5 #3)."""

    def __init__(self, message: str, *, platform: Platform, code: str | int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.platform = platform
        self.code = code
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"platform": self.platform.value, "code": self.code, "status": self.status, "message": str(self)}


def upstream_error(platform: Platform, message: str, *, status: int | None, retryable: bool | None = None,
                   code: str | int | None = None, subcode: int | None = None) -> UpstreamError:
    """Build the typed upstream error. Default retryability: network errors, 408, 425, 429 and 5xx."""
    if retryable is None:
        retryable = status is None or status in (408, 425, 429) or status >= 500
    return UpstreamError(f"{platform.value}: {message}", status=status, retryable=retryable,
                         platform=platform.value, code=code, subcode=subcode)


ErrorClassifier = Callable[[Platform, int, Any], Exception | None]


def classify_http_status(platform: Platform, status: int, body: Any) -> Exception | None:
    """Fallback classifier: only the HTTP status decides."""
    if status < 400:
        return None
    return upstream_error(platform, body_message(body) or f"HTTP {status}", status=status)


def body_message(body: Any) -> str:
    """Best-effort human message from an error body (never contains request secrets)."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("error_user_msg") or err.get("code") or "")
        if isinstance(err, str):
            return f"{err}: {body.get('error_description', '')}".strip(": ")
        return str(body.get("message") or "")
    return str(body or "")[:300]


def safe_json(resp: httpx.Response) -> Any:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


def as_dict(value: Any) -> dict[str, Any]:
    """The value if it is a JSON object, else an empty dict (defensive parsing of upstream bodies)."""
    return value if isinstance(value, dict) else {}


class ApiTransport:
    """One platform's HTTP channel: timeouts, typed error mapping, token-free error messages."""

    def __init__(self, platform: Platform, classify: ErrorClassifier = classify_http_status, *,
                 http: httpx.AsyncClient | None = None, timeout: float | httpx.Timeout = 30.0) -> None:
        self.platform = platform
        self._classify = classify
        self._http = http
        self._owns_http = http is None
        self._timeout = timeout
        install_log_redaction()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
                      data: Mapping[str, Any] | None = None, json_body: Any = None,
                      headers: Mapping[str, str] | None = None, content: bytes | None = None,
                      http_timeout: float | httpx.Timeout | None = None) -> httpx.Response:
        """Send one request; raise ``UpstreamError`` / ``PlatformPolicyWarning`` on failure signals."""
        kwargs: dict[str, Any] = {"params": params, "data": data, "headers": headers, "content": content}
        if json_body is not None:
            kwargs["json"] = json_body
        if http_timeout is not None:
            kwargs["timeout"] = http_timeout
        try:
            resp = await self.http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise upstream_error(self.platform, f"timeout on {method} {url_path(url)}", status=None) from exc
        except httpx.TransportError as exc:
            raise upstream_error(self.platform, f"{type(exc).__name__} on {method} {url_path(url)}",
                                 status=None) from exc
        body = safe_json(resp)
        mapped = self._classify(self.platform, resp.status_code, body)
        if mapped is not None:
            raise mapped
        if resp.status_code >= 400:
            raise upstream_error(self.platform, body_message(body) or f"HTTP {resp.status_code}",
                                 status=resp.status_code)
        return resp

    async def json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self.request(method, url, **kwargs)
        body = safe_json(resp)
        return body if isinstance(body, dict) else {}

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None


def require_id(platform: Platform, body: Mapping[str, Any], key: str = "id") -> str:
    """Extract an id from a success body or fail loudly (malformed upstream responses are not success)."""
    value = body.get(key)
    if value in (None, ""):
        raise upstream_error(platform, f"response missing {key!r}", status=502, retryable=False)
    return str(value)


# ───────────────────────────── token material ─────────────────────────────


@dataclass(slots=True)
class TokenBundle:
    """Token material behind ``ChannelAccount.token_ref``.

    The secret value is either a bare access token (``raw=True``) or JSON with ``access_token``,
    ``refresh_token``, ``expires_at`` (ISO-8601), ``client_id``, ``client_secret`` and ``token_type``.
    Unknown keys are preserved in ``extra`` and written back unchanged.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_type: str = "bearer"
    extra: dict[str, Any] = field(default_factory=dict)
    raw: bool = False

    def __repr__(self) -> str:  # never render secrets
        return (f"TokenBundle(token_type={self.token_type!r}, expires_at={self.expires_at!r}, "
                f"refreshable={self.refresh_token is not None}, raw={self.raw})")

    __str__ = __repr__

    @classmethod
    def parse(cls, value: str) -> TokenBundle:
        text = value.strip()
        if not text:
            raise ValueError("empty token secret")
        if not text.startswith("{"):
            return cls(access_token=text, raw=True)
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ValueError("token secret JSON has no access_token")
        expires_raw = data.pop("expires_at", None)
        expires_at = ensure_utc(datetime.fromisoformat(str(expires_raw))) if expires_raw else None
        return cls(access_token=str(data.pop("access_token")), refresh_token=data.pop("refresh_token", None),
                   expires_at=expires_at, client_id=data.pop("client_id", None),
                   client_secret=data.pop("client_secret", None),
                   token_type=str(data.pop("token_type", "bearer") or "bearer"), extra=data)

    def dumps(self) -> str:
        data: dict[str, Any] = dict(self.extra)
        data["access_token"] = self.access_token
        data["token_type"] = self.token_type
        for key in ("refresh_token", "client_id", "client_secret"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        return json.dumps(data, sort_keys=True)

    def renewed(self, access_token: str, *, now: datetime, expires_in: float | None,
                refresh_token: str | None = None, token_type: str | None = None,
                **extra: Any) -> TokenBundle:
        """Copy with fresh access material; client credentials and unknown keys are kept."""
        expires_at = now + timedelta(seconds=float(expires_in)) if expires_in else None
        return replace(self, access_token=access_token, expires_at=expires_at,
                       refresh_token=refresh_token or self.refresh_token,
                       token_type=token_type or self.token_type, extra={**self.extra, **extra}, raw=False)


class TokenRefreshFailed(ACVMError):
    """Token missing, unreadable, expired or rejected on refresh (§10 → account cooldown + alert)."""

    code = "token_refresh_failed"
    retryable = False

    def __init__(self, message: str, *, platform: Platform, account_id: str, reason: str) -> None:
        super().__init__(message, platform=platform.value, account_id=account_id, reason=reason)
        self.platform = platform
        self.account_id = account_id
        self.reason = reason


@runtime_checkable
class TokenRefresher(Protocol):
    """Implemented by platform clients whose platform supports programmatic token renewal."""

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle: ...


META_REFRESH_LEAD = timedelta(days=7)  # §10: "Meta 장기 토큰 만료 7일 전 자동 갱신"
SHORT_LIVED_SKEW = timedelta(minutes=5)  # Google (1 h) / TikTok (24 h) access tokens: refresh on expiry
_META = frozenset({Platform.THREADS, Platform.INSTAGRAM, Platform.FACEBOOK})


def refresh_lead(platform: Platform) -> timedelta:
    if platform in _META:
        return META_REFRESH_LEAD
    if platform in (Platform.YOUTUBE, Platform.TIKTOK):
        return SHORT_LIVED_SKEW
    return timedelta(0)


def needs_refresh(platform: Platform, bundle: TokenBundle, now: datetime) -> bool:
    """True when the token is inside its refresh lead window (tokens without expiry never refresh)."""
    if bundle.expires_at is None:
        return False
    return bundle.expires_at - now <= refresh_lead(platform)


class TokenManager:
    """Resolve a valid access token for an account, refreshing and writing back when due (§10)."""

    def __init__(self, ctx: AppContext, clients: Mapping[Platform, PlatformClient]) -> None:
        self.ctx = ctx
        self.clients = clients
        self._locks: dict[str, asyncio.Lock] = {}

    async def access_token(self, account: ChannelAccount) -> str:
        return (await self.bundle(account)).access_token

    async def bundle(self, account: ChannelAccount) -> TokenBundle:
        client = self.clients.get(account.platform)
        lock = self._locks.setdefault(account.token_ref, asyncio.Lock())
        async with lock:
            current = await self._load(account, client)
            now = self.ctx.clock.now()
            if not needs_refresh(account.platform, current, now):
                return current
            expired = current.expires_at is not None and current.expires_at <= now
            if not isinstance(client, TokenRefresher):
                if expired:
                    raise self._failed(account, "token expired and the platform client cannot refresh", "expired")
                return current
            try:
                fresh = await client.refresh_token(current, now)
            except UpstreamError as exc:
                if exc.retryable and not expired:
                    # still valid: keep using it, the next publish retries the refresh inside the lead window
                    log.warning("token_refresh_deferred", platform=account.platform.value,
                                account_id=str(account.id), error=str(exc))
                    return current
                if exc.retryable:
                    raise
                raise self._failed(account, f"refresh rejected: {exc}", "rejected") from exc
            except PlatformWarning as exc:
                raise self._failed(account, f"refresh refused by platform: {exc}", "policy") from exc
            except ACVMError as exc:
                if exc.retryable:
                    raise
                raise self._failed(account, f"refresh impossible: {exc}", "unrefreshable") from exc
            await self.ctx.secrets.put(account.token_ref, fresh.dumps())
            log.info("token_refreshed", platform=account.platform.value, account_id=str(account.id),
                     expires_at=fresh.expires_at.isoformat() if fresh.expires_at else None)
            return fresh

    async def _load(self, account: ChannelAccount, client: PlatformClient | None) -> TokenBundle:
        try:
            value = await self.ctx.secrets.get(account.token_ref)
        except KeyError:
            if getattr(client, "is_sandbox", False):
                # dry-run sandbox never sends the token anywhere; a placeholder keeps dev flows working
                return TokenBundle(access_token=f"sandbox-token:{account.id}", raw=True)
            raise self._failed(account, "token secret missing", "missing") from None
        try:
            return TokenBundle.parse(value)
        except (ValueError, TypeError) as exc:
            raise self._failed(account, f"token secret unreadable ({type(exc).__name__})", "unreadable") from None

    @staticmethod
    def _failed(account: ChannelAccount, message: str, reason: str) -> TokenRefreshFailed:
        return TokenRefreshFailed(message, platform=account.platform, account_id=str(account.id), reason=reason)
