"""Domain contracts (Pydantic). These are the *only* shapes that cross module boundaries — modules
exchange them inside events (§6.3) and persist them through `acvm.db`. Field names follow §6.1."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from acvm.core.enums import (
    AccountStatus,
    AssetSource,
    ContentKind,
    ContentStatus,
    HookType,
    Intent,
    LinkHealth,
    Platform,
    PublicationState,
    RunStatus,
    Verdict,
)
from acvm.core.ids import uuid7


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=False, extra="forbid")


# ───────────────────────────── operating units ─────────────────────────────


class Workspace(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    name: str
    plan: str = "internal"
    daily_budget_krw: float = 20_000.0
    created_at: datetime


class ChannelAccount(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    workspace_id: uuid.UUID
    platform: Platform
    handle: str
    platform_user_id: str = ""
    token_ref: str  # Vault path — the token itself is never stored (§6.1, §10)
    daily_cap: int
    status: AccountStatus = AccountStatus.ACTIVE
    cooldown_until: datetime | None = None
    niche: str
    persona_default: str = "30대 여성 직장인"
    created_at: datetime


# ───────────────────────────── M1 products ─────────────────────────────


class Product(_Model):
    id: int
    coupang_product_id: str
    name: str
    category_id: str
    category_name: str = ""
    price: int
    rating: float | None = None  # not returned by the Partners search API → operator/verified input
    review_count: int | None = None
    is_rocket: bool = False
    is_free_shipping: bool = False
    image_url: str = ""
    product_url: str = ""
    regulated: bool = False
    last_seen_at: datetime
    price_checked_at: datetime | None = None
    verified_facts: dict[str, Any] = Field(default_factory=dict)  # operator-verified specs (§4 M4 input)


class ProductFeatures(_Model):
    log_reviews: float
    rating: float
    rocket: float
    price_affinity: float
    r_cat: float
    v_demo: float
    m_trend: float
    epc_bayes: float
    p_risk: float
    imputed: list[str] = Field(default_factory=list)  # features filled from category priors


class ProductScore(_Model):
    product_id: int
    scored_at: datetime
    pos: float
    features: ProductFeatures
    model_version: str


# ───────────────────────────── M2 Content DNA ─────────────────────────────


class ContentDNA(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    source: Literal["own", "external_meta", "operator_note"]
    hook_type: HookType
    first3s_pattern: str = ""
    cut_count: int = 0
    avg_cut_sec: float = 0.0
    caption_cps: float = 0.0  # caption characters per second
    cta_position_sec: float = 0.0
    emotion_arc: list[str] = Field(default_factory=lambda: ["불편", "발견", "해소"])
    duration_sec: int = 29
    perf_percentile: float = 0.5
    reference_text: str = ""  # own content only; external references never store media/transcripts
    embedding: list[float] | None = None
    workspace_id: uuid.UUID | None = None


# ───────────────────────────── M4 scripts ─────────────────────────────

ClaimKind = Literal["fact", "price", "spec", "review", "shipping", "opinion", "cta", "disclosure"]
BeatRole = Literal["hook", "problem", "amplify", "reveal", "benefits", "cta"]


class Claim(_Model):
    """Every factual sentence carries pointers into the product facts that justify it (§4 M4)."""

    id: str
    text: str
    kind: ClaimKind
    evidence: list[str] = Field(default_factory=list)  # keys into ProductFacts
    grounded: bool = False


class ScriptBeat(_Model):
    start_sec: float
    end_sec: float
    role: BeatRole
    narration: str
    on_screen_text: str = ""
    visual: str = ""  # director instruction, e.g. "product_closeup", "problem_scene"
    claim_ids: list[str] = Field(default_factory=list)

    @field_validator("end_sec")
    @classmethod
    def _ordered(cls, v: float, info: Any) -> float:
        if v <= info.data.get("start_sec", 0.0):
            raise ValueError("beat end must be after start")
        return v


class Script(_Model):
    hooks: list[str]
    chosen_hook: int = 0
    hook_type: HookType
    beats: list[ScriptBeat] = Field(default_factory=list)
    body: str = ""  # text-kind post body (Threads / blog style)
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""
    disclosure: str = ""  # the slot M7 guarantees is filled and first
    title: str = ""
    duration_sec: float = 0.0
    persona: str
    template_id: str
    language: str = "ko"

    @property
    def hook(self) -> str:
        return self.hooks[self.chosen_hook] if self.hooks else ""

    @property
    def narration(self) -> str:
        return " ".join(b.narration for b in self.beats)


class RenderMeta(_Model):
    media_uri: str
    thumbnail_uri: str = ""
    duration_sec: float
    width: int
    height: int
    fps: int
    loudness_lufs: float | None = None
    disclosure_overlay: dict[str, Any] = Field(default_factory=dict)  # text/start/end/box for D-03
    asset_ids: list[uuid.UUID] = Field(default_factory=list)
    voice_id: str = ""
    voice_source: str = ""
    template_id: str = ""
    sha256: str = ""
    renderer: str = ""
    variants: dict[str, str] = Field(default_factory=dict)  # platform -> media uri


class Content(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    workspace_id: uuid.UUID
    product_id: int
    dna_id: uuid.UUID | None = None
    kind: ContentKind
    persona: str
    hook_variant: str = ""
    template_id: str
    script: Script
    claims: list[Claim] = Field(default_factory=list)
    status: ContentStatus = ContentStatus.DRAFT
    similarity_max: float = 0.0
    created_by_run: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    render: RenderMeta | None = None
    parent_content_id: uuid.UUID | None = None  # amplification lineage (M10)
    # Studio / API render choices (all optional; M6 auto-decides anything unset):
    #   voice_id, caption_style, caption_position (bottom|middle|upper), captions_enabled (bool),
    #   watermark (str), beat_assets {"<beat index>": "<ledger asset id>"}
    render_options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# ───────────────────────────── M5 rights ledger ─────────────────────────────


class Asset(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    kind: Literal["image", "video", "audio", "font", "voice"]
    uri: str
    sha256: str
    rights_source: AssetSource
    license: str
    expires_at: date | None = None
    product_id: int | None = None
    attribution: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


# ───────────────────────────── M7 compliance ─────────────────────────────


class Finding(_Model):
    rule_id: str
    verdict: Verdict
    message: str
    fixed: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ComplianceCheck(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    content_id: uuid.UUID
    rule_set_version: str
    verdict: Verdict
    findings: list[Finding]
    iteration: int = 1
    reviewer: uuid.UUID | None = None
    decided_at: datetime
    stage: Literal["gate", "preflight", "human"] = "gate"


# ───────────────────────────── M3 / M8 links & publications ─────────────────────────────


class AffiliateLink(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    product_id: int
    sub_id: str
    deeplink: str  # landing URL returned by the deeplink API (carries the partner tag)
    short_url: str  # Coupang short link (link.coupang.com/a/…)
    hub_code: str = ""  # our /r/{code} redirect for click logging
    health: LinkHealth = LinkHealth.OK
    checked_at: datetime | None = None
    signature: str  # HMAC(sub_id|deeplink|short_url|partner_id) — append-only integrity (§10)
    publication_key: str
    created_at: datetime


class Publication(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    content_id: uuid.UUID
    channel_account_id: uuid.UUID
    platform: Platform
    link_id: uuid.UUID
    platform_post_id: str = ""
    platform_container_id: str = ""
    state: PublicationState = PublicationState.QUEUED
    caption: str = ""
    link_placement: str = ""
    scheduled_at: datetime
    published_at: datetime | None = None
    idempotency_key: str
    attempts: int = 0
    error: dict[str, Any] | None = None
    ai_label: bool = True


# ───────────────────────────── orchestration ─────────────────────────────


class CommandSlots(_Model):
    """§5.3 slot grammar."""

    intent: Intent = Intent.CREATE_AND_PUBLISH
    category: str | None = None
    count: int = 5
    kind: ContentKind | None = None
    persona: str | None = None
    platforms: list[Platform] = Field(default_factory=list)
    schedule: str = "now"  # now | tonight | daily@HH:MM | ISO datetime
    budget_krw: float | None = None
    keyword: str | None = None


class PublishTarget(_Model):
    platform: Platform
    account_id: uuid.UUID
    account_handle: str
    link_placement: Literal["body", "first_comment", "hub", "caption_first_line", "manual"]
    format: str  # "text", "text+video", "reels", "shorts", "video"


class PlanStep(_Model):
    id: str
    module: Literal["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10"]
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class Approval(_Model):
    mode: Literal["auto", "console"]
    reasons: list[str] = Field(default_factory=list)


class Plan(_Model):
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    intent: Intent
    slots: CommandSlots
    category_id: str
    niche: str
    count: int
    min_pos: float = 0.7
    kind: ContentKind
    persona: str
    hooks: int = 3
    length_sec: int = 22  # fast 20–25 s product pitch
    template_id: str = "problem-solution-v3"
    voice_id: str = "ko-F-30s-warm"
    rule_set: str = "v1"
    targets: list[PublishTarget]
    schedule_window: tuple[int, int] = (9, 23)
    jitter_minutes: int = 7
    recurring_cron: str | None = None
    start_at: datetime | None = None
    est_cost_krw: float
    budget_cap_krw: float
    approval: Approval
    steps: list[PlanStep] = Field(default_factory=list)
    explanation: str = ""


class CommandRun(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    workspace_id: uuid.UUID
    raw_command: str
    plan: Plan | None = None
    status: RunStatus = RunStatus.PLANNED
    approved_by: str | None = None
    cost_krw: float = 0.0
    started_at: datetime
    ended_at: datetime | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────── M10 learning ─────────────────────────────


class ArmKey(_Model):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    hook_type: str
    template: str
    persona: str
    platform: str
    slot: str  # time-of-day bucket, e.g. "evening"

    def key(self) -> str:
        return "|".join((self.category, self.hook_type, self.template, self.persona, self.platform,
                         self.slot))

    @classmethod
    def parse(cls, key: str) -> ArmKey:
        c, h, t, p, pl, s = key.split("|")
        return cls(category=c, hook_type=h, template=t, persona=p, platform=pl, slot=s)


class ArmPosteriorState(_Model):
    arm_key: str
    ctr_alpha: float
    ctr_beta: float
    order_shape: float  # Gamma posterior on orders-per-click λ
    order_rate: float
    comm_shape: float  # Gamma posterior on θ, commission-per-order ~ Exp(θ)
    comm_rate: float
    impressions: float = 0.0
    clicks: float = 0.0
    orders: float = 0.0
    commission_krw: float = 0.0
    trials: int = 0
    retired: bool = False
    updated_at: datetime


class PlaybookRule(_Model):
    id: uuid.UUID = Field(default_factory=uuid7)
    workspace_id: uuid.UUID | None = None
    text: str
    scope: dict[str, str] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    lift: float = 0.0
    confidence: float = 0.0
    active: bool = True
    created_at: datetime


class AuditEntry(_Model):
    actor: str
    action: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime
