"""TTS providers and selection (§4 M6 "TTS(페르소나별 보이스 매핑) → 강제 정렬(word-level 타임스탬프)").

Providers (all implement :class:`acvm.ports.TTS` and return a monotone word-timing list whose length equals the
number of whitespace-separated words of the input chunks):

* :class:`ElevenLabsTTS` — native httpx client for ``/v1/text-to-speech/{voice}/with-timestamps``
  (``eleven_multilingual_v2``); character alignment is folded into 어절 timings. Voice ids come from the
  ``elevenlabs/voice_map`` secret; unmapped voices use the account's first available (premade) voice, looked up
  at runtime — no voice id is hard-coded.
* :class:`Yap1000Bridge` — reuses YAP-1000's providers (ElevenLabs with a key, deterministic MockTTS without)
  plus YAP's Korean normaliser; tokens are folded back onto the *original* 어절 so captions show "12,900원".
* :class:`acvm.media.sapi.WindowsSapiTTS` — Windows' built-in Korean voice (Heami) with SSML prosody.
* :class:`TimedToneTTS` — dependency-free deterministic stand-in (speech-shaped tone, syllable timing).

:func:`build_tts` order: injected ``ctx.tts`` → explicit preference ``ctx.extras["tts.provider"]`` (when
available) → ElevenLabs (key present; via YAP when the bridge is configured) → SAPI (Windows + Korean voice) →
YAP mock (bridge configured) → TimedToneTTS.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from acvm.core.errors import ConfigError, UpstreamError
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.media._yap import load_yap
from acvm.media.ffmpeg import MediaError, run
from acvm.media.korean import estimate_syllables, sino_korean
from acvm.media.sapi import WindowsSapiTTS, korean_sapi_voice
from acvm.media.voices import VOICE_CATALOG, elevenlabs_key, elevenlabs_voice_map, get_voice, voice_spec
from acvm.ports import TTS, TTSResult, WordTiming

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

SAMPLE_RATE = 48_000
SYLLABLES_PER_SECOND = 6.5
PREFERENCE_KEY = "tts.provider"  # ctx.extras key: "elevenlabs" | "sapi" | "yap-mock" | "acvm-tone" | "auto"
_CACHE_KEY = "m6.tts"
PROVIDERS = ("elevenlabs", "sapi", "yap-mock", "acvm-tone")

_SENT_END = re.compile(r"[.!?…。！？]['\"”’)]*$")
_CLAUSE_END = re.compile(r"[,，;:]['\"”’)]*$")

_sino_korean = sino_korean  # backwards-compatible private alias


# ───────────────────────────── offline stand-in ─────────────────────────────


class TimedToneTTS:
    """Deterministic offline TTS stand-in: speech-shaped tone bursts with syllable-rate timing."""

    provider = "acvm-tone"

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, syllables_per_second: float = SYLLABLES_PER_SECOND) -> None:
        self.sample_rate = sample_rate
        self.sps = syllables_per_second

    async def synthesize(self, chunks: Sequence[str], out_wav: Path, voice_id: str, *, speed: float = 1.0,
                         directives: Sequence[dict[str, Any]] | None = None) -> TTSResult:
        return await asyncio.to_thread(self._synthesize, list(chunks), Path(out_wav), voice_id, speed,
                                       list(directives or []))

    def _synthesize(self, chunks: list[str], out_wav: Path, voice_id: str, speed: float,
                    directives: list[dict[str, Any]]) -> TTSResult:
        sr = self.sample_rate
        vs = voice_spec(voice_id)
        seed = int.from_bytes(hashlib.blake2b(f"{voice_id}|{'␟'.join(chunks)}|{speed}".encode(),
                                              digest_size=8).digest(), "big")
        rng = np.random.default_rng(seed)
        pieces: list[np.ndarray] = [np.zeros(int(0.12 * sr), dtype=np.float64)]
        t = 0.12
        words: list[WordTiming] = []
        n_chars = 0
        for ci, chunk in enumerate(chunks):
            d = directives[ci] if ci < len(directives) else {}
            rate = self.sps * float(d.get("speed", speed)) * vs.rate_factor
            toks = chunk.split()
            for wi, w in enumerate(toks):
                syl = estimate_syllables(w)
                dur = max(0.16, syl / rate)
                pieces.append(self._voiced(syl, dur, vs.f0_hz, wi / max(1, len(toks) - 1), rng))
                words.append(WordTiming(w, round(t, 4), round(t + dur, 4)))
                t += dur
                n_chars += len(w)
                if _SENT_END.search(w):  # brisk short-form delivery: tight pauses
                    gap = 0.32
                elif _CLAUSE_END.search(w):
                    gap = 0.18
                else:
                    gap = 0.04
                if wi == len(toks) - 1:
                    gap = max(gap, 0.24)
                pieces.append(np.zeros(round(gap * sr), dtype=np.float64))
                t += gap
        pieces.append(np.zeros(int(0.2 * sr), dtype=np.float64))
        audio = np.concatenate(pieces)
        peak = float(np.max(np.abs(audio))) or 1.0
        pcm = np.clip(audio / peak * 0.6, -1.0, 1.0)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(sr)
            fh.writeframes((pcm * 32767.0).round().astype("<i2").tobytes())
        duration = round(len(audio) / sr, 4)
        return TTSResult(wav_path=out_wav, words=words, duration_s=duration, voice_id=voice_id,
                         provider=self.provider, usage={"chars": n_chars, "krw": 0.0, "offline": True})

    def _voiced(self, syllables: int, dur: float, f0: float, sentence_pos: float,
                rng: np.random.Generator) -> np.ndarray:
        sr = self.sample_rate
        n = max(1, round(dur * sr))
        tt = np.arange(n) / sr
        # declination over the clause + slow vibrato-like drift
        base = f0 * (1.06 - 0.10 * sentence_pos)
        inst = base * (1.0 + 0.035 * np.sin(2 * np.pi * 2.7 * tt + float(rng.uniform(0, 6.28))))
        phase = 2 * np.pi * np.cumsum(inst) / sr
        sig = np.zeros(n)
        for k in range(1, 9):
            fk = base * k
            formant = 1.0 + 1.6 * math.exp(-((fk - 700.0) / 350.0) ** 2) + 0.9 * math.exp(-((fk - 1900.0) / 500.0) ** 2)
            sig += (formant / k ** 1.15) * np.sin(k * phase)
        # syllable envelope: one rounded burst per syllable with a short dip between syllables
        pos = (tt / dur) * syllables
        frac = pos - np.floor(pos)
        env = np.sin(np.pi * np.clip(frac / 0.88, 0.0, 1.0)) ** 0.7
        edge = np.minimum(1.0, np.minimum(tt, dur - tt) / 0.012)
        noise = rng.normal(0.0, 0.03, n)
        voiced: np.ndarray = (sig / 3.0 + noise) * env * edge
        return voiced


# ───────────────────────────── token folding ─────────────────────────────


def regroup_tokens(original_words: Sequence[str], counts: Sequence[int],
                   tokens: Sequence[Mapping[str, Any]]) -> list[WordTiming]:
    """Fold provider tokens (of the normalised text) back onto the original 어절.

    ``counts[i]`` is how many normalised tokens original word ``i`` became. When the provider's token count
    matches, tokens are grouped exactly; otherwise times are interpolated along the character axis.
    """
    n = len(original_words)
    if n == 0:
        return []
    out: list[WordTiming] = []
    if tokens and len(tokens) == sum(max(1, c) for c in counts):
        k = 0
        for w, c in zip(original_words, counts, strict=True):
            group = tokens[k:k + max(1, c)]
            k += max(1, c)
            out.append(WordTiming(w, float(group[0]["start"]), float(group[-1]["end"])))
    elif tokens:
        tok_chars = [max(1, len(str(t["text"]))) for t in tokens]
        total_tok = float(sum(tok_chars))
        bounds = np.cumsum([0, *tok_chars], dtype=np.float64)

        def at(pos: float) -> float:
            idx = int(np.clip(np.searchsorted(bounds, pos, side="right") - 1, 0, len(tokens) - 1))
            tk = tokens[idx]
            frac = float(np.clip((pos - bounds[idx]) / tok_chars[idx], 0.0, 1.0))
            s, e = float(tk["start"]), float(tk["end"])
            return s + (e - s) * frac

        word_chars = [max(1, len(w)) for w in original_words]
        total_w = float(sum(word_chars))
        acc = 0.0
        for w, wc in zip(original_words, word_chars, strict=True):
            a = acc / total_w * total_tok
            acc += wc
            b = acc / total_w * total_tok
            out.append(WordTiming(w, at(a), at(max(a, b - 1e-6))))
    else:
        raise UpstreamError("TTS returned no word timings", retryable=True)
    fixed: list[WordTiming] = []
    prev_start = 0.0
    for wt in out:
        s = max(wt.start, prev_start)
        e = max(wt.end, s + 0.02)
        fixed.append(WordTiming(wt.text, round(s, 4), round(e, 4)))
        prev_start = s
    return fixed


def chars_to_word_tokens(chars: Sequence[str], starts: Sequence[float], ends: Sequence[float],
                         offset: float) -> list[dict[str, Any]]:
    """Fold ElevenLabs character alignment into whitespace-delimited word tokens."""
    toks: list[dict[str, Any]] = []
    cur, cs = "", None
    last_end = 0.0
    for ch, s, e in zip(chars, starts, ends, strict=False):
        if ch.isspace():
            if cur and cs is not None:
                toks.append({"text": cur, "start": round(cs + offset, 4), "end": round(last_end + offset, 4)})
            cur, cs = "", None
            continue
        if cs is None:
            cs = float(s)
        cur += ch
        last_end = float(e)
    if cur and cs is not None:
        toks.append({"text": cur, "start": round(cs + offset, 4), "end": round(last_end + offset, 4)})
    return toks


# ───────────────────────────── ElevenLabs (native) ─────────────────────────────


class ElevenLabsTTS:
    """ElevenLabs with-timestamps synthesis over httpx (async). The API key is never logged."""

    provider = "elevenlabs"
    BASE = "https://api.elevenlabs.io/v1"

    def __init__(self, api_key: str, *, voice_map: Mapping[str, str] | None = None,
                 model_id: str = "eleven_multilingual_v2", ffmpeg: str = "ffmpeg",
                 http: httpx.AsyncClient | None = None, time_limit: float = 120.0) -> None:
        self._key = api_key
        self.voice_map = dict(voice_map or {})
        self.model_id = model_id
        self.ffmpeg = ffmpeg
        self._http = http
        self.time_limit = time_limit
        self._default_voice: str | None = None

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._key, "Accept": "application/json"}

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        retryable = resp.status_code == 429 or resp.status_code >= 500
        raise UpstreamError(f"ElevenLabs {what} HTTP {resp.status_code}", status=resp.status_code, retryable=retryable)

    async def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=httpx.Timeout(self.time_limit)), True

    async def default_voice(self) -> str:
        """First premade voice of the account (cached) — used for logical voices without a mapping."""
        if self._default_voice:
            return self._default_voice
        if self.voice_map.get("default"):
            self._default_voice = self.voice_map["default"]
            return self._default_voice
        http, own = await self._client()
        try:
            resp = await http.get(f"{self.BASE}/voices", headers=self._headers())
            self._raise_for(resp, "voices")
            voices = list(resp.json().get("voices", []))
        finally:
            if own:
                await http.aclose()
        premade = [v for v in voices if v.get("category") == "premade"] or voices
        if not premade:
            raise UpstreamError("ElevenLabs account has no voices", retryable=False)
        self._default_voice = str(premade[0]["voice_id"])
        return self._default_voice

    async def resolve_voice(self, voice_id: str) -> str:
        return self.voice_map.get(voice_id) or await self.default_voice()

    async def synthesize(self, chunks: Sequence[str], out_wav: Path, voice_id: str, *, speed: float = 1.0,
                         directives: Sequence[dict[str, Any]] | None = None) -> TTSResult:
        texts = [c.strip() for c in chunks if c.strip()]
        if not texts:
            raise UpstreamError("nothing to synthesise", retryable=False)
        profile = voice_spec(voice_id)
        voice = await self.resolve_voice(voice_id)
        out = Path(out_wav)
        out.parent.mkdir(parents=True, exist_ok=True)
        work = out.parent / f".{out.stem}_el"
        work.mkdir(exist_ok=True)
        http, own = await self._client()
        tokens: list[dict[str, Any]] = []
        parts: list[Path] = []
        offset = 0.0
        try:
            for i, text in enumerate(texts):
                d = (directives[i] if directives and i < len(directives) else {}) or {}
                rate = max(0.7, min(1.2, float(d.get("speed", speed)) * profile.rate))
                payload = {"text": text, "model_id": self.model_id,
                           "previous_text": texts[i - 1][-500:] if i else "",
                           "next_text": texts[i + 1][:500] if i + 1 < len(texts) else "",
                           "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.2,
                                              "use_speaker_boost": True, "speed": round(rate, 3)}}
                try:
                    resp = await http.post(f"{self.BASE}/text-to-speech/{voice}/with-timestamps",
                                           params={"output_format": "mp3_44100_128"}, json=payload,
                                           headers=self._headers())
                except httpx.TimeoutException as exc:
                    raise UpstreamError("ElevenLabs synthesis timed out", retryable=True) from exc
                except httpx.TransportError as exc:
                    raise UpstreamError(f"ElevenLabs transport error: {type(exc).__name__}", retryable=True) from exc
                self._raise_for(resp, "synthesis")
                data = resp.json()
                mp3 = work / f"part_{i:03d}.mp3"
                wav = work / f"part_{i:03d}.wav"
                mp3.write_bytes(base64.b64decode(data["audio_base64"]))
                await run([self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(mp3), "-ac", "1",
                           "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(wav)], time_limit=self.time_limit)
                al = data.get("alignment") or data.get("normalized_alignment") or {}
                tokens += chars_to_word_tokens(al.get("characters", []), al.get("character_start_times_seconds", []),
                                               al.get("character_end_times_seconds", []), offset)
                with wave.open(str(wav), "rb") as fh:
                    offset += fh.getnframes() / float(fh.getframerate())
                parts.append(wav)
            _concat_wavs(parts, out)
        finally:
            if own:
                await http.aclose()
            for p in [*parts, *work.glob("*.mp3")]:
                p.unlink(missing_ok=True)
            if work.exists() and not any(work.iterdir()):
                work.rmdir()
        originals = [w for t in texts for w in t.split()]
        words = regroup_tokens(originals, [1] * len(originals), tokens)
        return TTSResult(wav_path=out, words=words, duration_s=round(offset, 4), voice_id=voice_id,
                         provider=self.provider, usage={"chars": sum(len(t) for t in texts), "voice": voice})


def _concat_wavs(parts: Sequence[Path], out: Path) -> None:
    with wave.open(str(out), "wb") as dst:
        for i, p in enumerate(parts):
            with wave.open(str(p), "rb") as src:
                if i == 0:
                    dst.setparams(src.getparams())
                dst.writeframes(src.readframes(src.getnframes()))


# ───────────────────────────── YAP-1000 bridge ─────────────────────────────


def _keyed_elevenlabs(providers: Any, api_key: str, model_id: str) -> Any:
    """Subclass YAP's ElevenLabsTTS so the key comes from ACVM's SecretStore instead of process env."""
    base = providers.ElevenLabsTTS
    http_json = providers._http_json
    provider_error = providers.ProviderError

    class KeyedElevenLabsTTS(base):  # type: ignore[misc, valid-type]
        def healthcheck(self) -> bool:
            return True

        def _chunk(self, text: str, voice: str, prev: str, nxt: str, speed: float = 1.0,
                   tone: str = "neutral") -> dict[str, Any]:
            url = f"{self.BASE}/text-to-speech/{voice}/with-timestamps"
            stability = {"urgent": 0.32, "warm": 0.55, "low": 0.68, "neutral": 0.45}.get(tone, 0.45)
            style = {"urgent": 0.42, "warm": 0.22, "low": 0.12, "neutral": 0.18}.get(tone, 0.18)
            payload = {"text": text, "model_id": self.model_id, "previous_text": prev[-500:], "next_text": nxt[:500],
                       "voice_settings": {"stability": stability, "similarity_boost": 0.75, "style": style,
                                          "use_speaker_boost": True, "speed": speed}}
            last: Exception | None = None
            for attempt in range(3):
                try:
                    out: dict[str, Any] = http_json(url, payload, {"xi-api-key": api_key})
                    return out
                except provider_error as exc:
                    msg = str(exc)
                    if msg.startswith("HTTP 4") and not msg.startswith("HTTP 429"):
                        raise
                    last = exc
                except OSError as exc:  # URLError / timeouts
                    last = exc
                time.sleep(min(8.0, 1.5 * 2 ** attempt))
            assert last is not None
            raise last

    return KeyedElevenLabsTTS(model_id=model_id)


class Yap1000Bridge:
    """TTS port backed by YAP-1000 (ElevenLabs with timestamps, or YAP MockTTS without a key)."""

    def __init__(self, yap1000_path: str, *, api_key: str | None = None, voice_map: Mapping[str, str] | None = None,
                 model_id: str = "eleven_multilingual_v2", default_voice: str | None = None) -> None:
        mods = load_yap(yap1000_path)
        if mods is None:
            raise ConfigError("YAP-1000 checkout is not importable", path=yap1000_path)
        self._textproc = mods.textproc
        self.voice_map = dict(voice_map or {})
        self._native: ElevenLabsTTS | None = None
        if api_key:
            self._engine = _keyed_elevenlabs(mods.providers, api_key, model_id)
            self._native = ElevenLabsTTS(api_key, voice_map=self.voice_map, model_id=model_id)
            if default_voice:
                self._native._default_voice = default_voice
            self.provider = "elevenlabs"
        else:
            self._engine = mods.providers.MockTTS()
            self.provider = "yap-mock"

    def _normalize(self, word: str) -> str:
        try:
            norm = str(self._textproc.normalize_for_tts(word)).strip()
        except Exception:  # normaliser bugs must not kill a render; read the word verbatim
            norm = word
        return norm or word

    async def synthesize(self, chunks: Sequence[str], out_wav: Path, voice_id: str, *, speed: float = 1.0,
                         directives: Sequence[dict[str, Any]] | None = None) -> TTSResult:
        originals: list[str] = []
        counts: list[int] = []
        norm_chunks: list[str] = []
        for chunk in chunks:
            parts: list[str] = []
            for w in chunk.split():
                norm = self._normalize(w)
                originals.append(w)
                counts.append(len(norm.split()))
                parts.append(norm)
            norm_chunks.append(" ".join(parts))
        provider_voice = await self._native.resolve_voice(voice_id) if self._native is not None else voice_id
        rate = speed * voice_spec(voice_id).rate
        out = Path(out_wav)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = await asyncio.to_thread(self._engine.synthesize, norm_chunks, str(out), provider_voice, rate,
                                          list(directives) if directives else None)
        except UpstreamError:
            raise
        except Exception as exc:
            msg = str(exc)[:300]
            retryable = not (msg.startswith("HTTP 4") and not msg.startswith("HTTP 429"))
            raise UpstreamError(f"YAP-1000 {self.provider} synthesis failed: {msg}", retryable=retryable) from exc
        words = regroup_tokens(originals, counts, list(res.tokens))
        usage = {"krw": float(getattr(res.usage, "krw", 0.0)), "units": float(getattr(res.usage, "units", 0.0)),
                 "detail": dict(getattr(res.usage, "detail", {}) or {}), "via": "yap1000"}
        return TTSResult(wav_path=Path(res.wav_path), words=words, duration_s=float(res.duration_s),
                         voice_id=voice_id, provider=self.provider, usage=usage)


# ───────────────────────────── selection ─────────────────────────────


async def _make(ctx: AppContext, provider: str) -> TTS | None:
    """Instantiate ``provider`` if it is usable in this environment, else None."""
    path = ctx.settings.render.yap1000_path
    yap_ok = bool(path) and load_yap(path or "") is not None
    if provider == "elevenlabs":
        key = await elevenlabs_key(ctx.secrets)
        if not key:
            return None
        vmap = await elevenlabs_voice_map(ctx.secrets)
        if yap_ok and path:
            return Yap1000Bridge(path, api_key=key, voice_map=vmap)
        return ElevenLabsTTS(key, voice_map=vmap, ffmpeg=ctx.settings.render.ffmpeg)
    if provider == "sapi":
        voice = await korean_sapi_voice()
        return WindowsSapiTTS(voice) if voice else None
    if provider == "yap-mock":
        return Yap1000Bridge(path) if yap_ok and path else None
    if provider == "acvm-tone":
        return TimedToneTTS()
    return None


async def build_tts(ctx: AppContext) -> TTS:
    """Pick the TTS for this context (see module docstring) and cache it in ``ctx.extras``."""
    if ctx.tts is not None:
        return ctx.tts
    pref = str(ctx.extras.get(PREFERENCE_KEY) or "auto")
    key = await elevenlabs_key(ctx.secrets)  # a key registered/removed in the console (any process) re-selects
    vmap = await elevenlabs_voice_map(ctx.secrets) if key else {}
    fp = stable_hash({"k": hashlib.blake2b((key or "").encode(), digest_size=6).hexdigest(), "m": vmap}, length=12)
    cached = ctx.extras.get(_CACHE_KEY)
    if isinstance(cached, tuple) and len(cached) == 3 and cached[0] == pref and cached[1] == fp:
        return cached[2]  # type: ignore[no-any-return]
    order = [pref, *PROVIDERS] if pref in PROVIDERS else list(PROVIDERS)
    tts: TTS | None = None
    for name in order:
        tts = await _make(ctx, name)
        if tts is not None:
            break
    assert tts is not None  # acvm-tone is always available
    if pref in PROVIDERS and tts.provider != pref:
        log.warning("tts_preference_unavailable", preferred=pref, using=tts.provider)
    log.info("tts_selected", provider=tts.provider)
    ctx.extras[_CACHE_KEY] = (pref, fp, tts)
    return tts


_STATUS_NOTES = {
    "elevenlabs": "ElevenLabs 연결됨 — 빠른 보이스 {n}종을 계정 음성에 자동 매핑해 사용 (운영 준비 › AI 연동 › 다시 매핑)",
    "sapi": "ElevenLabs 키 미등록 — Windows 기본 한국어 음성(Microsoft Heami, 1종)을 속도·높이로 변주해 {n}종으로 사용 "
            "(운영 준비 › AI 연동에서 키를 등록하면 즉시 실제 음성으로 전환)",
    "yap-mock": "실제 음성 엔진 없음 — YAP-1000 모의 음성(합성 톤)으로 길이·자막 타이밍만 맞춰 생성합니다. ElevenLabs 키를 등록하세요",
    "acvm-tone": "실제 음성 엔진 없음 — 합성 톤으로 길이·자막 타이밍만 맞춰 생성합니다 "
                 "(ElevenLabs 키 등록 또는 Windows 한국어 음성 필요)",
}


async def tts_status(ctx: AppContext) -> dict[str, Any]:
    """What the voice step can offer right now (Studio ④ 보이스)."""
    tts = await build_tts(ctx)
    provider = tts.provider
    ids = [v.id for v in VOICE_CATALOG]
    status: dict[str, Any] = {
        "provider": provider,
        "real_voice": provider in ("elevenlabs", "sapi"),
        "voices_available": ids,
        "note": _STATUS_NOTES.get(provider, "사용자 지정 TTS").format(n=len(ids)),
    }
    if provider == "elevenlabs":
        vmap = await elevenlabs_voice_map(ctx.secrets)
        status["mapped_voices"] = sorted(k for k in vmap if k in {v.id for v in VOICE_CATALOG})
    if provider == "sapi":
        status["engine_voice"] = getattr(tts, "voice_name", "")
    return status


async def synthesize_preview(ctx: AppContext, voice_id: str) -> tuple[bytes, str]:
    """▶ preview audio for a catalog voice (sample sentence), cached at ``voices/preview/{provider}/{voice_id}.wav``.

    The cache index lives in the KV table (key includes a hash of the sample text and prosody, so editing the
    catalog invalidates it); a missing object is re-synthesised.
    """
    profile = get_voice(voice_id)
    tts = await build_tts(ctx)
    version = stable_hash({"text": profile.sample_text, "rate": profile.rate, "pitch": profile.pitch_st,
                           "provider": tts.provider,
                           "mapped": str((getattr(tts, "voice_map", None) or {}).get(voice_id, ""))}, length=12)
    kv_key = f"voice_preview:{tts.provider}:{voice_id}:{version}"
    async with ctx.uow() as repos:
        hit = await repos.kv.get(kv_key, ctx.clock.now())
    if hit and hit.get("uri"):
        try:
            return await ctx.objects.get_bytes(str(hit["uri"])), "audio/wav"
        except Exception:  # object vanished → fall through and re-synthesise
            log.info("voice_preview_cache_miss", voice_id=voice_id)
    import tempfile
    from datetime import timedelta

    with tempfile.TemporaryDirectory(prefix="acvm_preview_", ignore_cleanup_errors=True) as td:
        res = await tts.synthesize([profile.sample_text], Path(td) / "preview.wav", voice_id)
        data = await asyncio.to_thread(Path(res.wav_path).read_bytes)
    uri = await ctx.objects.put_bytes(data, f"voices/preview/{tts.provider}/{voice_id}.wav", "audio/wav")
    async with ctx.uow() as repos:
        await repos.kv.put(kv_key, {"uri": uri, "bytes": len(data)}, timedelta(days=365), ctx.clock.now())
    return data, "audio/wav"


__all__ = [
    "PREFERENCE_KEY",
    "PROVIDERS",
    "ElevenLabsTTS",
    "MediaError",
    "TimedToneTTS",
    "Yap1000Bridge",
    "build_tts",
    "chars_to_word_tokens",
    "estimate_syllables",
    "regroup_tokens",
    "synthesize_preview",
    "tts_status",
]
