"""Studio voice catalog, persona → voice mapping and voice ledger entries (§4 M6 "TTS(페르소나별 보이스 매핑)",
§8.1 R-03 "보이스 출처 확인").

Shared by M5 (which registers voices in the rights ledger), M6 (which synthesises with them) and the Studio API
(voice cards with ▶ preview), so all three agree on the logical voice id without importing each other.

* Logical ids are provider-neutral (``ko-F-30s-warm``). ``VoiceProfile.provider_ids`` maps them to concrete
  provider voices; ElevenLabs ids are intentionally left empty here (they are account-specific and must be
  verified) and come from the ``elevenlabs/voice_map`` secret (JSON ``{"<logical id>": "<voice id>",
  "default": "<voice id>"}``); an unmapped voice falls back to the provider default voice.
* On Windows without an ElevenLabs key the only Korean voice is SAPI "Microsoft Heami Desktop"; catalog voices
  then differ by ``rate`` and ``pitch_st`` (SSML prosody), which is stated plainly in ``tts_status``.
* A voice is recorded in the ledger as ``Asset(kind="voice", uri="tts:<provider>:<voice_id>")`` with the
  provider's commercial licence. No cloned or celebrity voices are ever mapped.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from acvm.core.enums import AssetSource
from acvm.core.errors import ValidationFailed
from acvm.domain.models import Asset
from acvm.ports import SecretStore

if TYPE_CHECKING:
    from acvm.context import AppContext

DEFAULT_VOICE_ID = "ko-F-30s-brisk"
ELEVENLABS_KEY_REF = "elevenlabs/api_key"
ELEVENLABS_VOICE_MAP_REF = "elevenlabs/voice_map"
SAPI_KOREAN_VOICE = "Microsoft Heami Desktop"


@dataclass(frozen=True)
class VoiceProfile:
    """One selectable studio voice (a "voice card")."""

    id: str
    name: str  # Korean given name shown on the card
    gender: Literal["F", "M"]
    age_band: str  # "20s" | "30s" | "40s" | "50s"
    tone: str  # 활발한 / 차분한 / 아나운서 / 대화체 / 속삭임 / 발랄한 / 따뜻한 / 신뢰감
    description: str
    persona_tags: tuple[str, ...]
    provider_ids: dict[str, str] = field(default_factory=dict)  # provider → concrete voice ("" = provider default)
    rate: float = 1.0  # relative speaking rate (1.0 = provider normal)
    pitch_st: float = 0.0  # pitch offset in semitones (SSML prosody)
    f0_hz: float = 205.0  # fundamental for the offline tone synthesiser
    sample_text: str = "안녕하세요, 오늘 소개할 제품은 매일 쓰기 좋은 살림템이에요."

    @property
    def voice_id(self) -> str:  # backwards-compatible alias (VoiceSpec.voice_id)
        return self.id

    @property
    def rate_factor(self) -> float:  # backwards-compatible alias (VoiceSpec.rate_factor)
        return self.rate

    def provider_voice(self, provider: str) -> str:
        return self.provider_ids.get(provider, "")


def _providers() -> dict[str, str]:
    """Concrete voices per provider. ElevenLabs is resolved from the ``elevenlabs/voice_map`` secret at runtime."""
    return {"elevenlabs": "", "sapi": SAPI_KOREAN_VOICE}


# Shorts are 20–25 s product pitches: every catalog voice is FAST and CONCISE (rate ≥ FAST_MIN_RATE).
# Slow narrator styles (차분한·따뜻한·속삭임·신뢰감·다정한 낭독 톤) were removed on the operator's directive;
# their old ids survive only as aliases to a fast replacement (LEGACY_ALIASES) so existing drafts keep rendering.
FAST_MIN_RATE = 1.2

VOICE_CATALOG: tuple[VoiceProfile, ...] = (
    VoiceProfile("ko-F-30s-brisk", "서윤", "F", "30s", "경쾌한 대화체",
                 "30대 여성의 빠르고 경쾌한 대화체. 살림·주방 쇼츠의 기본 보이스.",
                 ("30대", "여성", "직장인", "살림", "주방"), _providers(), rate=1.22, pitch_st=1, f0_hz=210.0,
                 sample_text="이거 하나면 끝! 주방 꿀템 바로 보여드릴게요."),
    VoiceProfile("ko-F-30s-rapid", "지현", "F", "30s", "속사포 정보형",
                 "핵심만 빠르게 짚는 정보형 톤. 비교·스펙 정리 쇼츠에 강해요.",
                 ("정보", "비교", "스펙", "리뷰", "아나운서"), _providers(), rate=1.28, pitch_st=2, f0_hz=214.0,
                 sample_text="핵심만 딱 세 가지! 구성, 리뷰, 배송까지 바로 정리할게요."),
    VoiceProfile("ko-F-20s-bright", "하은", "F", "20s", "발랄한",
                 "20대 여성의 밝고 빠른 톤. 자취템·뷰티·간식 쇼츠에 잘 어울려요.",
                 ("20대", "여성", "대학생", "자취", "뷰티"), _providers(), rate=1.25, pitch_st=3, f0_hz=228.0,
                 sample_text="자취생 필수템 등장! 이거 진짜 편해요, 바로 볼게요!"),
    VoiceProfile("ko-F-20s-energetic", "다인", "F", "20s", "하이텐션",
                 "텐션 높은 속도감 있는 톤. 가격 반전·특가 훅에 가장 강해요.",
                 ("20대", "특가", "가성비", "텐션", "가격"), _providers(), rate=1.3, pitch_st=4, f0_hz=236.0,
                 sample_text="잠깐만요! 이 구성 이 가격, 지금 바로 확인해요!"),
    VoiceProfile("ko-F-40s-lively", "미소", "F", "40s", "생기있는",
                 "40대 여성의 생기 있고 빠른 살림 톤. 육아·대용량 생활용품에 어울려요.",
                 ("40대", "50대", "여성", "주부", "육아", "엄마", "대용량"), _providers(), rate=1.2, pitch_st=0,
                 f0_hz=196.0, sample_text="살림하는 분들 주목! 매일 쓰는 거라 더 편해야죠."),
    VoiceProfile("ko-M-30s-brisk", "준호", "M", "30s", "빠른 대화체",
                 "30대 남성의 빠르고 친근한 대화체. 차량·캠핑·공구 쇼츠의 기본 보이스.",
                 ("30대", "남성", "차량", "캠핑", "직장인"), _providers(), rate=1.22, pitch_st=-7, f0_hz=118.0,
                 sample_text="차에 두면 진짜 자주 씁니다. 바로 보여드릴게요!"),
    VoiceProfile("ko-M-30s-rapid", "태오", "M", "30s", "속사포 리뷰",
                 "속도감 있는 남성 리뷰 톤. 전자기기·가전 핵심 정리에 적합.",
                 ("30대", "남성", "가전", "전자기기", "공구", "리뷰"), _providers(), rate=1.3, pitch_st=-6,
                 f0_hz=122.0, sample_text="딱 15초 요약! 이 제품 핵심 세 가지 갑니다."),
    VoiceProfile("ko-M-20s-energetic", "민재", "M", "20s", "활발한",
                 "20대 남성의 활기차고 빠른 톤. 게임·자취·남성 타깃 쇼츠에 적합.",
                 ("20대", "남성", "게임", "자취", "전자기기"), _providers(), rate=1.28, pitch_st=-5, f0_hz=128.0,
                 sample_text="책상 위 이거 하나로 확 달라져요. 바로 갑니다!"),
    VoiceProfile("ko-M-40s-punchy", "상훈", "M", "40s", "힘있는 단문",
                 "40대 남성의 힘 있고 짧게 끊는 톤. 차량·아빠템·고가 제품 쇼츠에 어울려요.",
                 ("40대", "50대", "남성", "아빠", "차량"), _providers(), rate=1.2, pitch_st=-9, f0_hz=106.0,
                 sample_text="오래 쓸 물건이면 이것부터 보세요. 짧게 정리합니다."),
)

# removed slow voices → fast replacements (never offered for new content)
LEGACY_ALIASES: dict[str, str] = {
    "ko-F-30s-warm": "ko-F-30s-brisk", "ko-F-30s-announcer": "ko-F-30s-rapid", "ko-F-40s-calm": "ko-F-40s-lively",
    "ko-F-50s-gentle": "ko-F-40s-lively", "ko-F-20s-whisper": "ko-F-20s-bright", "ko-M-30s-friendly": "ko-M-30s-brisk",
    "ko-M-30s-calm": "ko-M-30s-rapid", "ko-M-40s-trust": "ko-M-40s-punchy", "ko-M-50s-warm": "ko-M-40s-punchy",
}

_slow = [v.id for v in VOICE_CATALOG if v.rate < FAST_MIN_RATE]
if _slow:  # import-time invariant: a slow voice can never be (re)introduced silently
    raise RuntimeError(f"voices slower than FAST_MIN_RATE={FAST_MIN_RATE}: {_slow}")

VOICES: dict[str, VoiceProfile] = {v.id: v for v in VOICE_CATALOG}
VoiceSpec = VoiceProfile  # backwards-compatible name

_MALE = re.compile(r"남성|남자|남\b|아빠|아저씨|male|\bM\b", re.IGNORECASE)
_AGE = re.compile(r"([1-6])0\s*대")
_TONE_HINTS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"아나운서|뉴스|정보형|스펙|비교"), "ko-F-30s-rapid", "ko-M-30s-rapid"),
    (re.compile(r"텐션|활발|특가|하이|가격"), "ko-F-20s-energetic", "ko-M-20s-energetic"),
)


def voice_for_persona(persona: str) -> VoiceProfile:
    """Map a free-text persona ("30대 여성 직장인", "20대 대학생 여자", "40대 남성", "아나운서 톤") to a FAST catalog voice.

    Explicit tone hints win (정보형·아나운서 / 텐션·특가); otherwise gender then age band decide. Requests for slow
    styles (ASMR·속삭임·차분) still resolve to a fast voice — shorts are 20–25 s pitches.
    """
    p = persona.strip()
    male = bool(_MALE.search(p))
    for pattern, female_id, male_id in _TONE_HINTS:
        if pattern.search(p):
            return VOICES[male_id if male else female_id]
    m = _AGE.search(p)
    decade = int(m.group(1)) * 10 if m else 30
    if male:
        if decade <= 20:
            return VOICES["ko-M-20s-energetic"]
        if decade >= 40:
            return VOICES["ko-M-40s-punchy"]
        return VOICES["ko-M-30s-brisk"]
    if decade <= 20:
        return VOICES["ko-F-20s-bright"]
    if decade >= 40:
        return VOICES["ko-F-40s-lively"]
    return VOICES[DEFAULT_VOICE_ID]


def voice_id_for_persona(persona: str) -> str:
    return voice_for_persona(persona).id


def voice_spec(voice_id: str) -> VoiceProfile:
    """Lenient lookup for synthesis internals: legacy slow ids → their fast replacement; unknown → default."""
    return VOICES.get(LEGACY_ALIASES.get(voice_id, voice_id), VOICES[DEFAULT_VOICE_ID])


def get_voice(voice_id: str) -> VoiceProfile:
    """Strict lookup for user-supplied ids (Studio override). Raises ValidationFailed for unknown voices."""
    try:
        return VOICES[LEGACY_ALIASES.get(voice_id, voice_id)]
    except KeyError:
        raise ValidationFailed("unknown voice id", voice_id=voice_id, known=sorted(VOICES)) from None


def voice_uri(provider: str, voice_id: str) -> str:
    return f"tts:{provider}:{voice_id}"


def voice_license(provider: str) -> str:
    return f"{provider} commercial TTS license"


def voice_asset(provider: str, voice_id: str, *, now: datetime | None = None) -> Asset:
    """Ledger row for a synthetic voice. sha256 is over the URI, so (sha256, rights_source) de-duplicates."""
    uri = voice_uri(provider, voice_id)
    return Asset(kind="voice", uri=uri, sha256=hashlib.sha256(uri.encode()).hexdigest(),
                 rights_source=AssetSource.LICENSED_STOCK, license=voice_license(provider),
                 attribution=f"synthetic voice {voice_id} via {provider}", tags=["voice", f"voice:{voice_id}"],
                 created_at=now)


async def elevenlabs_key(secrets: SecretStore) -> str | None:
    """The ElevenLabs API key if configured (never logged)."""
    try:
        value = await secrets.get(ELEVENLABS_KEY_REF)
    except (KeyError, LookupError):
        return None
    except Exception:  # a secret-backend outage degrades to the offline voice instead of failing the render
        return None
    return value or None


async def elevenlabs_voice_map(secrets: SecretStore) -> dict[str, str]:
    """Logical voice id → ElevenLabs voice id from the ``elevenlabs/voice_map`` secret (JSON object)."""
    try:
        raw = await secrets.get(ELEVENLABS_VOICE_MAP_REF)
        data = json.loads(raw)
    except Exception:  # absent or malformed map → provider default voice for every logical id
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def yap_importable(path: str | None) -> bool:
    """True when the YAP-1000 checkout at ``path`` can be imported (bridge enabled)."""
    if not path:
        return False
    from acvm.media._yap import load_yap

    return load_yap(path) is not None


async def resolve_tts_provider(ctx: AppContext) -> str:
    """Provider name the render path will use — exactly the decision of :func:`acvm.media.tts.build_tts`."""
    from acvm.media.tts import build_tts

    return (await build_tts(ctx)).provider


async def synthesize_preview(ctx: AppContext, voice_id: str) -> tuple[bytes, str]:
    """▶ preview for a voice card: the voice's sample sentence with the current TTS provider.

    Cached in the object store at ``voices/preview/{provider}/{voice_id}.wav``. Raises ValidationFailed for an
    unknown voice id.
    """
    from acvm.media.tts import synthesize_preview as _impl

    return await _impl(ctx, voice_id)
