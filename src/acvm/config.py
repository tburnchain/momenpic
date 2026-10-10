"""Runtime configuration.

Every tunable named in the design document lives here with the design value as default, so the
numbers in code and in the spec cannot drift apart silently. Secrets never live here — only
*references* (Vault paths / env names) that `acvm.security.secrets` resolves at use time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "test", "staging", "prod"]


class EconomicsDefaults(BaseModel):
    """§2.5 example assumptions — replaced by measured values as M9 data accumulates."""

    c_unit_short_krw: float = 150.0  # LLM + TTS 30s + render, image generation excluded
    c_unit_text_krw: float = 30.0  # LLM only
    ctr: float = 0.008
    cvr: float = 0.03
    aov_krw: float = 30_000.0
    commission_rate: float = 0.03
    lognormal_sigma: float = 2.0  # §2.2 heavy-tail assumption until fitted per channel


class CostModel(BaseModel):
    """§9.4 unit costs (KRW). Replace with contract prices."""

    llm_per_score_krw: float = 2.0
    llm_per_script_krw: float = 30.0
    llm_per_critic_krw: float = 18.0
    tts_per_30s_krw: float = 50.0
    render_per_short_krw: float = 0.0  # absorbed by the server cost
    storage_per_short_krw: float = 2.0


class QuotaPolicy(BaseModel):
    """§7 operating caps per account per day — deliberately 2–30 % of the API limit."""

    daily_cap: dict[str, int] = Field(
        default_factory=lambda: {
            "threads": 20,
            "instagram": 5,
            "youtube": 3,
            "tiktok": 3,
            "facebook": 3,
            "kakao": 10,
        }
    )
    api_limit_24h: dict[str, int] = Field(
        default_factory=lambda: {
            "threads": 250,
            "instagram": 100,
            "youtube": 100,
            "tiktok": 15,
            "facebook": 30,
            "kakao": 10_000,
        }
    )
    max_api_fraction: float = 0.30
    jitter_minutes: int = 7
    avoid_top_of_hour_minutes: int = 3
    min_gap_minutes: int = 20
    window_start_hour: int = 9
    window_end_hour: int = 23
    cooldown_hours: int = 48
    failure_rate_threshold: float = 0.10
    failure_confidence: float = 0.80
    reach_drop_threshold: float = -0.60


class GatePolicy(BaseModel):
    """§8 thresholds. The optimizer has no write path to this object (§8 preamble)."""

    rule_set_version: str = "v1"
    standard_disclosure: str = (
        "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )
    video_disclosure: str = "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다"
    disclosure_overlay_seconds: float = 3.0
    reference_cosine_max: float = 0.80
    reference_ngram_max: float = 0.15
    reference_ngram_n: int = 5
    account_minhash_max: float = 0.60
    account_lookback_days: int = 30
    new_account_hold_days: int = 14
    new_template_hold_count: int = 10
    max_fix_iterations: int = 3
    price_fresh_minutes: int = 360


class OptimizerPolicy(BaseModel):
    """§4 M10 parameters."""

    exploration_budget: float = 0.20
    reward_horizon_days: int = 7
    amplify_window_hours: int = 48
    amplify_percentile: float = 0.98
    amplify_variants: int = 5
    retire_min_trials: int = 30
    retire_percentile: float = 0.10
    prior_strength: float = 20.0  # pseudo-observations for hierarchical shrinkage


class RenderSpec(BaseModel):
    """§4 M6 output contract."""

    width: int = 1080
    height: int = 1920
    fps: int = 30
    target_lufs: float = -14.0
    true_peak: float = -1.5
    # Shorts are 20–25 s product pitches (operator directive 2026-10-08, tightening §4 M6's 15–45 s):
    # fast, concise narration only — slow "narrator" delivery is never allowed.
    min_seconds: float = 20.0
    max_seconds: float = 25.0
    min_speech_sps: float = 7.5  # floor on measured speaking rate (Korean syllables / second of speech span)
    target_speech_sps: float = 8.0  # rate the timeline speeds narration up to when a voice comes out slower
    max_tempo: float = 1.35  # largest atempo correction before the script is rejected as too long
    concurrency: int = 8
    font_family: str = "Noto Sans CJK KR"
    font_file: str | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    yap1000_path: str | None = None  # path to the YAP-1000 checkout; enables the reuse bridge


class CoupangConfig(BaseModel):
    base_url: str = "https://api-gateway.coupang.com"
    access_key_ref: str = "coupang/access_key"
    secret_key_ref: str = "coupang/secret_key"
    partner_id: str = ""  # AF####### — used to verify deeplinks before publishing (§10)
    search_calls_per_hour: int = 8
    catalog_calls_per_hour: int = 8  # bestcategories + goldbox share one bucket (limit documented for search only)
    deeplink_calls_per_minute: int = 30
    report_calls_per_hour: int = 30
    cache_ttl_seconds: int = 6 * 3600
    breaker_open_seconds: int = 3600
    sandbox: bool = True  # fixture-backed client until G0 (§11 P0)


class LLMConfig(BaseModel):
    # auto: Claude as soon as its key exists (env or console), Gemini when only a Gemini key exists, else offline;
    # with both keys Gemini takes over automatically when Claude fails. "gemini" forces Gemini.
    backend: Literal["anthropic", "gemini", "offline", "auto"] = "auto"
    model_large: str = "claude-opus-5-5"
    model_medium: str = "claude-sonnet-5-5"
    model_small: str = "claude-haiku-4-5"
    server_fallbacks: bool = True
    max_retries: int = 3
    timeout_seconds: float = 120.0


class GeminiConfig(BaseModel):
    """Google Gemini (Generative Language API, REST) — LLM fallback/alternative, vision and STT."""

    key_ref: str = "gemini/api_key"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model_large: str = "gemini-3.8-flash"
    model_medium: str = "gemini-3.8-flash"
    model_small: str = "gemini-3.5-flash-lite"
    timeout_seconds: float = 120.0
    max_retries: int = 2
    input_krw_per_mtok: float = 700.0  # placeholder list price → replace with the contract price
    output_krw_per_mtok: float = 4_200.0


class OpenAIImageConfig(BaseModel):
    """OpenAI GPT Image — scene backgrounds for non-product beats (never the product itself, §4 M5)."""

    key_ref: str = "openai/api_key"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-image-2.5-flare"
    size: str = "1024x1536"  # portrait, multiples of 16; cropped to 9:16 by the renderer
    quality: Literal["low", "medium", "high", "auto"] = "medium"
    timeout_seconds: float = 180.0
    cost_per_image_krw: float = 90.0  # placeholder → contract price


class HiggsfieldConfig(BaseModel):
    """Higgsfield (api.higgsfield.ai) image→video — animates AI scene stills only, never product images."""

    key_id_ref: str = "higgsfield/key_id"
    key_secret_ref: str = "higgsfield/key_secret"
    base_url: str = "https://api.higgsfield.ai"
    i2v_model: str = "kling-video/v2.5-turbo/standard/image-to-video"
    clip_seconds: int = 5
    cfg_scale: float = 0.5
    poll_timeout_seconds: float = 480.0
    timeout_seconds: float = 60.0
    cost_per_clip_krw: float = 450.0  # placeholder → contract price (credits × unit price)


class ElevenLabsConfig(BaseModel):
    """ElevenLabs TTS. The key and the voice map live in the secret store (``elevenlabs/api_key``, ``voice_map``)."""

    model_id: str = "eleven_multilingual_v2"
    auto_voice_map: bool = True  # map the fast catalog voices to account voices as soon as the key is registered
    krw_per_1k_chars: float = 300.0


class AIVisualPolicy(BaseModel):
    """When GPT Image / Higgsfield keys are active: AI scene media for *scene* beats only (problem/amplify)."""

    max_images_per_content: int = 2
    max_clips_per_content: int = 2
    animate_scenes: bool = True  # Higgsfield clip from the scene still when that provider is active
    label_text: str = "AI 생성 장면"  # on-screen label on beats that show generated imagery (AI 기본법 표시)


class EmbeddingConfig(BaseModel):
    backend: Literal["voyage", "hashing"] = "hashing"
    model: str = "voyage-3.5"
    dim: int = 1024
    api_key_ref: str = "voyage/api_key"


class ObjectStoreConfig(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local_root: str = "var/objects"
    endpoint_url: str = ""
    bucket: str = "acvm-media"
    public_base_url: str = "http://localhost:8000/media"
    access_key_ref: str = "r2/access_key"
    secret_key_ref: str = "r2/secret_key"
    signed_url_ttl_seconds: int = 7200


class PlatformConfig(BaseModel):
    graph_version: str = "v24.0"
    threads_base: str = "https://graph.threads.net/v1.0"
    graph_base: str = "https://graph.facebook.com"
    rupload_base: str = "https://rupload.facebook.com"
    youtube_upload_base: str = "https://www.googleapis.com/upload/youtube/v3"
    youtube_base: str = "https://www.googleapis.com/youtube/v3"
    tiktok_base: str = "https://open.tiktokapis.com"
    tiktok_audited: bool = False  # §7: public posting is gated on audit approval
    dry_run: bool = True  # sandbox adapters record instead of calling until G0/G1


class SecurityConfig(BaseModel):
    # env: process env (+ console values in memory) · file: env first, then <data_dir>/secrets.json (single host,
    # console-entered keys survive restarts; refused in prod) · vault: HashiCorp Vault (prod)
    secret_backend: Literal["env", "vault", "file"] = "env"
    vault_addr: str = "http://127.0.0.1:8200"
    vault_mount: str = "acvm"
    vault_role_id_env: str = "ACVM_VAULT_ROLE_ID"
    vault_secret_id_env: str = "ACVM_VAULT_SECRET_ID"
    secret_cache_ttl_seconds: int = 300  # §10: 5-minute dynamic credential TTL
    link_signing_key_ref: str = "acvm/link_signing_key"
    click_ip_salt_ref: str = "acvm/click_ip_salt"
    # dev: static bearer token (developer machines) · session: member login (email + password, HttpOnly cookie)
    # · cloudflare_access: identity from Cloudflare Access JWT
    auth_mode: Literal["dev", "session", "cloudflare_access"] = "dev"
    signup_mode: Literal["approval", "open", "closed"] = "approval"  # approval: owner activates new members
    session_cookie: str = "acvm_session"
    session_ttl_hours: int = 24 * 14
    login_max_failures: int = 5  # per account, then locked for login_lock_minutes
    login_lock_minutes: int = 15
    login_ip_limit_per_10min: int = 30
    service_token: str = ""  # optional machine token for scripts in session mode (empty = disabled)
    cf_access_team_domain: str = ""
    cf_access_audience: str = ""
    dev_api_token: str = "dev-owner-token"
    audit_retention_days: int = 365


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACVM_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    env: Env = "dev"
    timezone: str = "Asia/Seoul"
    data_dir: Path = Path("var")
    database_url: str = "sqlite+aiosqlite:///./var/acvm.db"
    redis_url: str | None = None
    clickhouse_url: str | None = None
    orchestrator: Literal["local", "temporal"] = "local"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "acvm"
    temporal_task_queue: str = "acvm-main"  # CommandWorkflow + light activities (no ffmpeg, no social tokens)
    temporal_render_queue: str = "acvm-render"  # produce_product: script · render · gate · links (ffmpeg)
    temporal_publish_queue: str = "acvm-publish"  # PublishWorkflow: the only queue whose workers read social tokens
    link_hub_base_url: str = "http://localhost:8000"
    console_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]  # CORS for the operations console
    otel_endpoint: str | None = None
    workspace_daily_budget_krw: float = 20_000.0
    auto_approve_account_age_days: int = 14

    economics: EconomicsDefaults = EconomicsDefaults()
    costs: CostModel = CostModel()
    quota: QuotaPolicy = QuotaPolicy()
    gate: GatePolicy = GatePolicy()
    optimizer: OptimizerPolicy = OptimizerPolicy()
    render: RenderSpec = RenderSpec()
    coupang: CoupangConfig = CoupangConfig()
    llm: LLMConfig = LLMConfig()
    gemini: GeminiConfig = GeminiConfig()
    openai_image: OpenAIImageConfig = OpenAIImageConfig()
    higgsfield: HiggsfieldConfig = HiggsfieldConfig()
    elevenlabs: ElevenLabsConfig = ElevenLabsConfig()
    ai_visuals: AIVisualPolicy = AIVisualPolicy()
    embeddings: EmbeddingConfig = EmbeddingConfig()
    objects: ObjectStoreConfig = ObjectStoreConfig()
    platforms: PlatformConfig = PlatformConfig()
    security: SecurityConfig = SecurityConfig()

    def assert_production_safe(self) -> None:
        """Refuse to boot prod with dev shortcuts left on."""
        if self.env != "prod":
            return
        problems: list[str] = []
        if self.security.auth_mode == "dev":
            problems.append("security.auth_mode=dev")
        if self.security.secret_backend != "vault":
            problems.append("security.secret_backend must be vault")
        if self.coupang.sandbox:
            problems.append("coupang.sandbox=true")
        if not self.coupang.partner_id:
            problems.append("coupang.partner_id unset")
        if self.database_url.startswith("sqlite"):
            problems.append("database_url is sqlite")
        if problems:
            raise RuntimeError("unsafe production configuration: " + ", ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
