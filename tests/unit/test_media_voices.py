"""Studio voice catalog (④ 보이스): structure, persona mapping and provider id resolution."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

import pytest

from acvm.core.errors import ValidationFailed
from acvm.infra.secrets import EnvSecretStore
from acvm.media.voices import (
    DEFAULT_VOICE_ID,
    FAST_MIN_RATE,
    LEGACY_ALIASES,
    VOICE_CATALOG,
    VOICES,
    VoiceProfile,
    elevenlabs_voice_map,
    get_voice,
    voice_for_persona,
    voice_id_for_persona,
    voice_spec,
)


def test_catalog_shape() -> None:
    ids = [v.id for v in VOICE_CATALOG]
    assert len(ids) >= 8 and len(set(ids)) == len(ids)
    assert DEFAULT_VOICE_ID in ids
    assert {v.gender for v in VOICE_CATALOG} == {"F", "M"}
    for v in VOICE_CATALOG:
        assert re.fullmatch(r"[가-힣]{2,3}", v.name), v.name  # Korean given names
        assert v.tone and v.description and v.persona_tags and v.sample_text
        assert v.provider_ids.get("elevenlabs") == ""  # never hard-coded; comes from the voice_map secret
        assert v.provider_ids.get("sapi")
        assert FAST_MIN_RATE <= v.rate <= 1.35 and -12 <= v.pitch_st <= 6
        json.dumps(asdict(v), ensure_ascii=False)  # API serialises cards with dataclasses.asdict
    assert len({v.name for v in VOICE_CATALOG}) == len(VOICE_CATALOG)
    assert len({(v.rate, v.pitch_st) for v in VOICE_CATALOG}) == len(VOICE_CATALOG)  # SAPI variants are distinct


def test_only_fast_voices_exist() -> None:
    """Shorts are 20–25 s product pitches: no narration-paced voice may be offered, chosen or synthesised."""
    assert FAST_MIN_RATE >= 1.2
    assert all(v.rate >= FAST_MIN_RATE for v in VOICE_CATALOG)
    for old, new in LEGACY_ALIASES.items():  # removed slow voices only survive as aliases to a fast voice
        assert old not in VOICES and VOICES[new].rate >= FAST_MIN_RATE
        assert voice_spec(old).id == new and get_voice(old).id == new
    for persona in ("ASMR 속삭임 20대", "차분한 50대 여성", "따뜻한 내레이션 40대 남성", "", "아무거나"):
        assert voice_for_persona(persona).rate >= FAST_MIN_RATE


def test_persona_mapping_returns_profiles() -> None:
    v = voice_for_persona("30대 여성 직장인")
    assert isinstance(v, VoiceProfile) and v.id == voice_id_for_persona("30대 여성 직장인") == "ko-F-30s-brisk"
    assert voice_for_persona("뉴스 아나운서 느낌 40대 남성").id == "ko-M-30s-rapid"
    assert voice_for_persona("특가 텐션 20대 여자").id == "ko-F-20s-energetic"
    assert voice_for_persona("텐션 높은 20대 남자 활발").gender == "M"


def test_lookup_strict_and_lenient() -> None:
    assert get_voice("ko-F-20s-bright").tone == "발랄한"
    assert get_voice("ko-F-20s-whisper").id == "ko-F-20s-bright"  # legacy slow id → fast replacement
    with pytest.raises(ValidationFailed):
        get_voice("someone-famous")
    assert voice_spec("unknown").id == DEFAULT_VOICE_ID


async def test_elevenlabs_voice_map_from_secret() -> None:
    assert await elevenlabs_voice_map(EnvSecretStore({})) == {}
    s = EnvSecretStore({"elevenlabs/voice_map": '{"ko-F-30s-warm": "abc", "x": ""}'})
    assert await elevenlabs_voice_map(s) == {"ko-F-30s-warm": "abc"}
    assert await elevenlabs_voice_map(EnvSecretStore({"elevenlabs/voice_map": "not json"})) == {}
