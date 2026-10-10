"""Closed vocabularies shared by every module (mirrors the CHECK constraints in §6)."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    THREADS = "threads"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    KAKAO = "kakao"

    @property
    def has_official_publish_api(self) -> bool:
        return self is not Platform.KAKAO

    @property
    def link_clickable_in_post(self) -> bool:
        """§4 M3 placement table: only Threads/Facebook make caption links clickable."""
        return self in (Platform.THREADS, Platform.FACEBOOK)


class ContentKind(StrEnum):
    TEXT = "text"
    SHORT = "short"


class ContentStatus(StrEnum):
    DRAFT = "draft"
    RENDERED = "rendered"
    GATED = "gated"
    APPROVED = "approved"
    BLOCKED = "blocked"


class Verdict(StrEnum):
    """§4 M7: exactly four outcomes. Ordered by severity for aggregation."""

    PASS = "PASS"
    FIX = "FIX"
    HOLD = "HOLD"
    BLOCK = "BLOCK"

    @property
    def severity(self) -> int:
        return _VERDICT_SEVERITY[self]

    @classmethod
    def worst(cls, verdicts: list[Verdict]) -> Verdict:
        return max(verdicts, key=lambda v: v.severity, default=cls.PASS)


_VERDICT_SEVERITY = {Verdict.PASS: 0, Verdict.FIX: 1, Verdict.HOLD: 2, Verdict.BLOCK: 3}


class PublicationState(StrEnum):
    QUEUED = "QUEUED"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    MANUAL_PENDING = "MANUAL_PENDING"  # kakao one-click queue (no official API)
    CANCELLED = "CANCELLED"
    TAKEN_DOWN = "TAKEN_DOWN"  # saga compensation (§5.2)

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = {
    PublicationState.PUBLISHED,
    PublicationState.FAILED,
    PublicationState.EXPIRED,
    PublicationState.CANCELLED,
    PublicationState.TAKEN_DOWN,
}


class AccountStatus(StrEnum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    RESTRICTED = "restricted"


class Role(StrEnum):
    OWNER = "owner"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class HookType(StrEnum):
    """§4 M2 DNA schema hook vocabulary."""

    QUESTION = "question"
    SHOCK = "shock"
    BEFORE_AFTER = "before_after"
    PRICE_TWIST = "price_twist"
    EMPATHY = "empathy"


class Intent(StrEnum):
    CREATE_AND_PUBLISH = "create_and_publish"
    CREATE_ONLY = "create_only"
    ANALYZE = "analyze"


class LinkHealth(StrEnum):
    OK = "ok"
    SOLD_OUT = "sold_out"
    DISCONTINUED = "discontinued"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class AssetSource(StrEnum):
    """§4 M5 priority order — lower index wins."""

    OWN_FOOTAGE = "own_footage"
    COUPANG_PRODUCT_IMAGE = "coupang_product_image"
    LICENSED_STOCK = "licensed_stock"
    AI_GENERATED_BACKGROUND = "ai_generated_background"
    GENERATED_CARD = "generated_card"  # deterministic typographic card made by ACVM itself

    @property
    def priority(self) -> int:
        return list(AssetSource).index(self)
