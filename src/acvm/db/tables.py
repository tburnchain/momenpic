"""§6.1 transactional schema (PostgreSQL 16 + pgvector). The ORM is the single source of truth;
PostgreSQL-only hardening (extension, immutability triggers, ANN indexes) lives in the migration.

Join key between the transactional and analytical stores is ``content_id``; revenue attribution is
guaranteed by ``publication.link_id UNIQUE`` + ``affiliate_link.sub_id UNIQUE`` (subId ↔ publication 1:1).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from acvm.db.types import EmbeddingType, JSONType, UTCDateTime

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType, datetime: UTCDateTime}  # noqa: RUF012


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


PLATFORMS = ("threads", "instagram", "facebook", "youtube", "tiktok", "kakao")
VERDICTS = ("PASS", "FIX", "HOLD", "BLOCK")
PUB_STATES = ("QUEUED", "UPLOADING", "PROCESSING", "PUBLISHED", "FAILED", "EXPIRED", "MANUAL_PENDING",
              "CANCELLED", "TAKEN_DOWN")


# ───────────── operating units ─────────────


class WorkspaceRow(Base):
    __tablename__ = "workspace"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    plan: Mapped[str] = mapped_column(Text, default="internal")
    daily_budget_krw: Mapped[float] = mapped_column(Float, default=20_000.0)
    created_at: Mapped[datetime]


class AppUserRow(Base):
    """Console member. ``password_hash`` is a scrypt PHC-style string (never the password); members who sign up
    start ``pending`` until an owner approves them (session auth mode); Cloudflare Access rows need no password."""

    __tablename__ = "app_user"
    __table_args__ = (_enum_check("role", ("owner", "operator", "reviewer", "viewer"), "role"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True, default="active")  # pending|active|disabled
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    failed_logins: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)


class AuthSessionRow(Base):
    """Login session behind the HttpOnly console cookie. Only the SHA-256 of the cookie value is stored."""

    __tablename__ = "auth_session"
    __table_args__ = (Index("ix_auth_session_user", "user_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)


class ChannelAccountRow(Base):
    __tablename__ = "channel_account"
    __table_args__ = (
        _enum_check("platform", PLATFORMS, "platform"),
        _enum_check("status", ("active", "cooldown", "restricted"), "status"),
        UniqueConstraint("platform", "handle"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16))
    handle: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text, default="")
    token_ref: Mapped[str] = mapped_column(Text)  # Vault path; raw tokens are never stored
    daily_cap: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    cooldown_until: Mapped[datetime | None]
    niche: Mapped[str] = mapped_column(Text)
    persona_default: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]


class AccountHealthEventRow(Base):
    __tablename__ = "account_health_event"
    __table_args__ = (Index("ix_ahe_account_at", "account_id", "at"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
                                    autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel_account.id"))
    kind: Mapped[str] = mapped_column(String(24))  # attempt_ok | attempt_fail | warning | reach
    value: Mapped[float] = mapped_column(Float, default=1.0)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime]


# ───────────── M1 products ─────────────


class ProductRow(Base):
    __tablename__ = "product"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    coupang_product_id: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    category_id: Mapped[str] = mapped_column(String(16), index=True)
    category_name: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    is_rocket: Mapped[bool] = mapped_column(Boolean, default=False)
    is_free_shipping: Mapped[bool] = mapped_column(Boolean, default=False)
    image_url: Mapped[str] = mapped_column(Text, default="")
    product_url: Mapped[str] = mapped_column(Text, default="")
    regulated: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime]
    price_checked_at: Mapped[datetime | None]
    verified_facts: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ProductSnapshotRow(Base):
    """Price/review history — feeds C-03 (price freshness) and M_trend (7-day velocity)."""

    __tablename__ = "product_snapshot"
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    at: Mapped[datetime] = mapped_column(primary_key=True)
    price: Mapped[int] = mapped_column(Integer)
    review_count: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)


class ProductScoreRow(Base):
    __tablename__ = "product_score"
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    scored_at: Mapped[datetime] = mapped_column(primary_key=True)
    pos: Mapped[float] = mapped_column(Float)
    features: Mapped[dict[str, Any]]
    model_version: Mapped[str] = mapped_column(String(32))


class SearchKeywordRow(Base):
    """Keyword queue for the rate-limited search API (§4 M1)."""

    __tablename__ = "search_keyword"
    keyword: Mapped[str] = mapped_column(Text, primary_key=True)
    category_id: Mapped[str] = mapped_column(String(16), default="")
    priority: Mapped[float] = mapped_column(Float, default=0.5)
    last_queried_at: Mapped[datetime | None]
    hits: Mapped[int] = mapped_column(Integer, default=0)


class KVCacheRow(Base):
    __tablename__ = "kv_cache"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]]
    expires_at: Mapped[datetime] = mapped_column(index=True)


# ───────────── M2 DNA ─────────────


class ContentDNARow(Base):
    __tablename__ = "content_dna"
    __table_args__ = (_enum_check("source", ("own", "external_meta", "operator_note"), "source"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    source: Mapped[str] = mapped_column(String(16))
    hook_type: Mapped[str] = mapped_column(String(16))
    first3s_pattern: Mapped[str] = mapped_column(Text, default="")
    cut_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_cut_sec: Mapped[float] = mapped_column(Float, default=0)
    caption_cps: Mapped[float] = mapped_column(Float, default=0)
    cta_position_sec: Mapped[float] = mapped_column(Float, default=0)
    emotion_arc: Mapped[list[Any]] = mapped_column(default=list)
    duration_sec: Mapped[int] = mapped_column(Integer, default=29)
    perf_percentile: Mapped[float] = mapped_column(Float, default=0.5)
    reference_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(1024))


# ───────────── M4–M6 content ─────────────


class ContentRow(Base):
    __tablename__ = "content"
    __table_args__ = (
        _enum_check("kind", ("text", "short"), "kind"),
        _enum_check("status", ("draft", "rendered", "gated", "approved", "blocked"), "status"),
        Index("ix_content_account_created", "target_account_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    dna_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("content_dna.id"))
    kind: Mapped[str] = mapped_column(String(8))
    persona: Mapped[str] = mapped_column(Text)
    hook_variant: Mapped[str] = mapped_column(Text, default="")
    template_id: Mapped[str] = mapped_column(String(64))
    script: Mapped[dict[str, Any]]
    claims: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    similarity_max: Mapped[float] = mapped_column(Float, default=0.0)
    created_by_run: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("channel_account.id"))
    render: Mapped[dict[str, Any] | None]
    parent_content_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    render_options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    text_for_similarity: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(1024))
    created_at: Mapped[datetime]


class AssetRow(Base):
    """§4 M5 rights ledger — an asset absent here can never reach the renderer (R-01)."""

    __tablename__ = "asset"
    __table_args__ = (UniqueConstraint("sha256", "rights_source"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    rights_source: Mapped[str] = mapped_column(String(32))
    license: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[date | None] = mapped_column(Date)
    product_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    attribution: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime | None]


class ContentAssetRow(Base):
    __tablename__ = "content_asset"
    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.id"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)


# ───────────── M7 compliance ─────────────


class ComplianceCheckRow(Base):
    __tablename__ = "compliance_check"
    __table_args__ = (_enum_check("verdict", VERDICTS, "verdict"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.id"), index=True)
    rule_set_version: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(8))
    findings: Mapped[list[Any]]
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(16), default="gate")
    reviewer: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[datetime]


class ReviewItemRow(Base):
    """HOLD queue (§8.4: ~20 s per item, ~10 min/day)."""

    __tablename__ = "review_item"
    __table_args__ = (_enum_check("status", ("pending", "approved", "rejected"), "status"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), index=True)
    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.id"), unique=True)
    check_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("compliance_check.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reasons: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]


# ───────────── M3 / M8 ─────────────


class AffiliateLinkRow(Base):
    """Immutable after insert except health/checked_at (PostgreSQL trigger enforces it, §10)."""

    __tablename__ = "affiliate_link"
    __table_args__ = (_enum_check("health", ("ok", "sold_out", "discontinued", "unknown"), "health"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    sub_id: Mapped[str] = mapped_column(String(32), unique=True)
    deeplink: Mapped[str] = mapped_column(Text)
    short_url: Mapped[str] = mapped_column(Text)
    hub_code: Mapped[str] = mapped_column(String(16), unique=True)
    signature: Mapped[str] = mapped_column(String(64))
    publication_key: Mapped[str] = mapped_column(Text, unique=True)
    health: Mapped[str] = mapped_column(String(16), default="ok")
    checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class PublicationRow(Base):
    __tablename__ = "publication"
    __table_args__ = (
        _enum_check("state", PUB_STATES, "state"),
        _enum_check("platform", PLATFORMS, "platform"),
        Index("ix_publication_account_sched", "channel_account_id", "scheduled_at"),
        Index("ix_publication_state_sched", "state", "scheduled_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.id"), index=True)
    channel_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel_account.id"))
    platform: Mapped[str] = mapped_column(String(16))
    link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("affiliate_link.id"), unique=True)
    platform_post_id: Mapped[str] = mapped_column(Text, default="")
    platform_container_id: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(16), default="QUEUED")
    caption: Mapped[str] = mapped_column(Text, default="")
    link_placement: Mapped[str] = mapped_column(String(24), default="")
    scheduled_at: Mapped[datetime]
    published_at: Mapped[datetime | None]
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[dict[str, Any] | None]
    ai_label: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime | None]


class PublicationTransitionRow(Base):
    __tablename__ = "publication_transition"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
                                    autoincrement=True)
    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("publication.id"), index=True)
    from_state: Mapped[str] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime]


# ───────────── orchestration (§5) ─────────────


class CommandRunRow(Base):
    __tablename__ = "command_run"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), index=True)
    raw_command: Mapped[str] = mapped_column(Text)
    plan: Mapped[dict[str, Any] | None]
    status: Mapped[str] = mapped_column(String(24), index=True)
    approved_by: Mapped[str | None] = mapped_column(Text)
    cost_krw: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_krw: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]


class RecurringCommandRow(Base):
    """§5.2 예약 실행 — "매일 오전 9시에 차량용품 10개"."""

    __tablename__ = "recurring_command"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"))
    raw_command: Mapped[str] = mapped_column(Text)
    cron: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(Text)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime]


class CostLedgerRow(Base):
    __tablename__ = "cost_ledger"
    __table_args__ = (Index("ix_cost_ws_at", "workspace_id", "at"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
                                    autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    content_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    stage: Mapped[str] = mapped_column(String(32))
    amount_krw: Mapped[float] = mapped_column(Float)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime]


class ActivityResultRow(Base):
    """§5.2 idempotency: a retried activity with the same key returns the stored result."""

    __tablename__ = "activity_result"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    stage: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


class OutboxRow(Base):
    """Transactional outbox: events commit atomically with the state change that caused them."""

    __tablename__ = "outbox"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(index=True)
    published_at: Mapped[datetime | None] = mapped_column(index=True)


# ───────────── M10 ─────────────


class ArmPosteriorRow(Base):
    __tablename__ = "arm_posterior"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    arm_key: Mapped[str] = mapped_column(Text, primary_key=True)
    ctr_alpha: Mapped[float] = mapped_column(Float)
    ctr_beta: Mapped[float] = mapped_column(Float)
    order_shape: Mapped[float] = mapped_column(Float)
    order_rate: Mapped[float] = mapped_column(Float)
    comm_shape: Mapped[float] = mapped_column(Float)
    comm_rate: Mapped[float] = mapped_column(Float)
    impressions: Mapped[float] = mapped_column(Float, default=0)
    clicks: Mapped[float] = mapped_column(Float, default=0)
    orders: Mapped[float] = mapped_column(Float, default=0)
    commission_krw: Mapped[float] = mapped_column(Float, default=0)
    trials: Mapped[int] = mapped_column(Integer, default=0)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime]


class ArmObservationRow(Base):
    """One row per (publication) reward observation — makes posterior updates idempotent/replayable."""

    __tablename__ = "arm_observation"
    publication_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    arm_key: Mapped[str] = mapped_column(Text, index=True)
    impressions: Mapped[float] = mapped_column(Float)
    clicks: Mapped[float] = mapped_column(Float)
    orders: Mapped[float] = mapped_column(Float)
    commission_krw: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime]


class PlaybookRuleRow(Base):
    __tablename__ = "playbook_rule"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    text: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    lift: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(1024))
    created_at: Mapped[datetime]


# ───────────── security ─────────────


class AuditLogRow(Base):
    """Append-only (PostgreSQL trigger rejects UPDATE/DELETE outside the retention role), 1-year TTL."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_at", "at"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
                                    autoincrement=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime]
