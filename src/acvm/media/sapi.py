"""Windows SAPI text-to-speech (System.Speech) — a real Korean voice for local/dev without paid keys.

Windows ships "Microsoft Heami Desktop" (ko-KR). :class:`WindowsSapiTTS` drives it through Windows PowerShell
(``System.Speech.Synthesis.SpeechSynthesizer``). Speed is set through the engine's own ``Rate`` property
(System.Speech ignores SSML ``<prosody rate>`` percentages; each ``Rate`` step is ×3^(1/10) ≈ 1.116) and pitch
through SSML ``<prosody pitch>``, so the fast studio catalog voices really are fast. It captures **word boundaries** from ``SpeakProgress`` events (character position
in the SSML + audio offset). Audio is written at 16 kHz mono, the engine's native rate — at that rate the
reported ``AudioPosition`` offsets are exact (other output rates scale them incorrectly).

Text never reaches a command line: the SSML is written to a UTF-8 file and the script reads it, so arbitrary
script text cannot inject PowerShell. Every call is an async subprocess (:func:`acvm.media.ffmpeg.run`).
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import math
import shutil
import sys
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from acvm.core.errors import UpstreamError
from acvm.media.ffmpeg import MediaError, run
from acvm.media.korean import estimate_syllables
from acvm.media.voices import SAPI_KOREAN_VOICE, voice_spec
from acvm.ports import TTSResult, WordTiming

SAPI_SAMPLE_RATE = 16_000
CHUNK_BREAK_MS = 350
_HEAMI_SYLLABLES_PER_SECOND = 4.7  # Heami at Rate 0 (measured articulation rate, word-boundary events)
NATURAL_SYLLABLES_PER_SECOND = 6.5  # the pace a catalog ``rate`` of 1.0 means (same base as the offline voice)
_RATE_STEP = 3.0 ** 0.1  # SAPI: Rate +10 ≈ 3× faster

_PS_SCRIPT = r"""
param([string]$Mode = 'speak', [string]$InFile = '', [string]$OutWav = '', [string]$Voice = '', [int]$Rate = 16000,
      [int]$Speed = 0)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($Mode -eq 'list') {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  foreach ($v in $s.GetInstalledVoices()) {
    if ($v.Enabled) { $i = $v.VoiceInfo; [Console]::Out.WriteLine(("{0}`t{1}`t{2}" -f $i.Name, $i.Culture.Name, $i.Gender)) }
  }
  $s.Dispose()
  exit 0
}
$src = @"
using System; using System.Text; using System.Speech.Synthesis; using System.Speech.AudioFormat;
public static class AcvmSapi {
  public static string Run(string ssml, string wav, string voice, int rate, int speed) {
    var sb = new StringBuilder();
    using (var s = new SpeechSynthesizer()) {
      if (!String.IsNullOrEmpty(voice)) { s.SelectVoice(voice); }
      s.Rate = Math.Max(-10, Math.Min(10, speed));
      s.SetOutputToWaveFile(wav, new SpeechAudioFormatInfo(rate, AudioBitsPerSample.Sixteen, AudioChannel.Mono));
      s.SpeakProgress += (o, e) => {
        sb.Append(e.CharacterPosition).Append('\t').Append(e.CharacterCount).Append('\t')
          .Append((long)e.AudioPosition.TotalMilliseconds).Append('\n');
      };
      s.SpeakSsml(ssml);
      s.SetOutputToNull();
    }
    return sb.ToString();
  }
}
"@
Add-Type -TypeDefinition $src -ReferencedAssemblies System.Speech
$ssml = [System.IO.File]::ReadAllText($InFile, [System.Text.Encoding]::UTF8)
[Console]::Out.Write([AcvmSapi]::Run($ssml, $OutWav, $Voice, $Rate, $Speed))
"""


@dataclass(frozen=True, slots=True)
class SapiVoice:
    name: str
    culture: str
    gender: str


_voices_cache: list[SapiVoice] | None = None


def powershell_exe() -> str | None:
    """Windows PowerShell 5.1 (``powershell.exe``) — it ships System.Speech; PowerShell 7 does not."""
    if sys.platform != "win32":
        return None
    return shutil.which("powershell") or shutil.which("powershell.exe")


def _script_path() -> Path:
    digest = hashlib.blake2b(_PS_SCRIPT.encode(), digest_size=6).hexdigest()
    path = Path(tempfile.gettempdir()) / "acvm-sapi" / f"acvm_sapi_{digest}.ps1"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{id(path)}.tmp")
        tmp.write_text(_PS_SCRIPT, encoding="utf-8-sig")  # BOM: PowerShell 5.1 reads non-ASCII scripts correctly
        tmp.replace(path)
    return path


def _ps_args(exe: str, *args: str) -> list[str]:
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(_script_path()), *args]


async def sapi_voices(*, refresh: bool = False) -> list[SapiVoice]:
    """Installed, enabled SAPI voices (empty off Windows). Cached for the process lifetime."""
    global _voices_cache
    if _voices_cache is not None and not refresh:
        return list(_voices_cache)
    exe = powershell_exe()
    if exe is None:
        _voices_cache = []
        return []
    try:
        res = await run(_ps_args(exe, "-Mode", "list"), time_limit=60)
    except MediaError:
        _voices_cache = []
        return []
    voices = []
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3:
            voices.append(SapiVoice(*parts))
    _voices_cache = voices
    return list(voices)


async def korean_sapi_voice() -> str | None:
    """Name of an installed ko-KR SAPI voice (Heami preferred), or None."""
    voices = await sapi_voices()
    names = [v.name for v in voices if v.culture.lower().startswith("ko")]
    if SAPI_KOREAN_VOICE in names:
        return SAPI_KOREAN_VOICE
    return names[0] if names else None


def _u16(s: str) -> int:
    """Length in UTF-16 code units (.NET string indices)."""
    return len(s.encode("utf-16-le")) // 2


def engine_rate(speed: float) -> int:
    """SAPI ``Rate`` step (−10…10) whose speed is closest to ``speed`` × the natural pace, relative to Heami's
    default. Rounds up on ties so a fast catalog voice never comes out slower than asked."""
    want = max(0.1, speed) * NATURAL_SYLLABLES_PER_SECOND / _HEAMI_SYLLABLES_PER_SECOND
    return max(-10, min(10, math.ceil(math.log(want, _RATE_STEP) - 0.5)))


def engine_speed(step: int) -> float:
    """Speed multiplier of a SAPI ``Rate`` step relative to Rate 0."""
    return float(_RATE_STEP ** step)


def build_ssml(chunks: Sequence[str], *, pitch_st: float = 0.0,
               lang: str = "ko-KR") -> tuple[str, list[str], list[tuple[int, int]]]:
    """SSML for ``chunks`` plus the original words and each word's [start, end) span (UTF-16 offsets).

    Speed is not in the SSML (System.Speech ignores relative prosody rates) — see :func:`engine_rate`.
    """
    head = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{lang}'>"
            f"<prosody pitch='{round(pitch_st):+d}st'>")
    parts = [head]
    pos = _u16(head)
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    for ci, chunk in enumerate(chunks):
        toks = chunk.split()
        if not toks:
            continue
        if ci and words:
            brk = f" <break time='{CHUNK_BREAK_MS}ms'/> "
            parts.append(brk)
            pos += _u16(brk)
        for wi, w in enumerate(toks):
            if wi:
                parts.append(" ")
                pos += 1
            esc = escape(w, {"'": "&apos;", '"': "&quot;"})
            spans.append((pos, pos + _u16(esc)))
            words.append(w)
            parts.append(esc)
            pos += _u16(esc)
    parts.append("</prosody></speak>")
    return "".join(parts), words, spans


def parse_progress(stdout: str) -> list[tuple[int, int, float]]:
    """``pos\\tcount\\tms`` lines → [(char_pos, char_count, seconds)]."""
    out = []
    for line in stdout.splitlines():
        bits = line.strip().split("\t")
        if len(bits) != 3:
            continue
        try:
            out.append((int(bits[0]), int(bits[1]), int(bits[2]) / 1000.0))
        except ValueError:
            continue
    return out


def align_words(words: Sequence[str], spans: Sequence[tuple[int, int]], events: Sequence[tuple[int, int, float]],
                duration: float, *, rate: float = 1.0) -> list[WordTiming]:
    """Word timings from SpeakProgress events; words the engine merged/skipped are interpolated, ends are capped
    by an estimated spoken length so pauses are not swallowed by the previous word."""
    n = len(words)
    if n == 0:
        return []
    starts: list[float | None] = [None] * n
    span_starts = [s for s, _ in spans]
    for pos, _cnt, t in events:
        k = bisect.bisect_right(span_starts, pos) - 1
        if 0 <= k < n and pos < spans[k][1] + 1 and starts[k] is None:
            starts[k] = t
    syl = [estimate_syllables(w) for w in words]
    speed = _HEAMI_SYLLABLES_PER_SECOND * max(0.5, rate)
    if all(s is None for s in starts):  # no events: proportional syllable timing over the speech span
        total = sum(syl)
        t0, t1 = 0.08, max(0.2, duration - 0.25)
        acc = 0.0
        for i, sy in enumerate(syl):
            starts[i] = t0 + (t1 - t0) * acc / total
            acc += sy
    known = [i for i, s in enumerate(starts) if s is not None]
    for i in range(n):  # interpolate gaps by syllable share
        if starts[i] is not None:
            continue
        lo = max((k for k in known if k < i), default=None)
        hi = min((k for k in known if k > i), default=None)
        if lo is not None and hi is not None:
            a, b = starts[lo], starts[hi]
            assert a is not None and b is not None
            span = sum(syl[lo:hi])
            starts[i] = a + (b - a) * sum(syl[lo:i]) / max(1, span)
        elif lo is not None:
            a = starts[lo]
            assert a is not None
            starts[i] = a + sum(syl[lo:i]) / speed
        else:
            b = starts[hi] if hi is not None else duration
            assert b is not None
            starts[i] = max(0.0, b - sum(syl[i:hi]) / speed)
    filled = [float(x) if x is not None else 0.0 for x in starts]
    out: list[WordTiming] = []
    prev = 0.0
    for i, w in enumerate(words):
        s = max(prev, filled[i])
        est = syl[i] / speed * 1.2 + 0.06
        nxt = filled[i + 1] if i + 1 < n else duration
        e = min(s + est, nxt - 0.02 if i + 1 < n else duration)
        e = max(e, s + 0.05)
        out.append(WordTiming(w, round(s, 4), round(min(e, max(duration, s + 0.05)), 4)))
        prev = s
    return out


class WindowsSapiTTS:
    """TTS port backed by Windows SAPI (System.Speech): engine ``Rate`` + SSML pitch per catalog voice."""

    provider = "sapi"

    def __init__(self, voice_name: str = SAPI_KOREAN_VOICE, *, powershell: str | None = None,
                 time_limit: float = 180.0) -> None:
        exe = powershell or powershell_exe()
        if exe is None:
            raise UpstreamError("Windows PowerShell is required for SAPI TTS", retryable=False)
        self.exe = exe
        self.voice_name = voice_name
        self.time_limit = time_limit

    async def synthesize(self, chunks: Sequence[str], out_wav: Path, voice_id: str, *, speed: float = 1.0,
                         directives: Sequence[dict[str, Any]] | None = None) -> TTSResult:
        profile = voice_spec(voice_id)
        step = engine_rate(max(0.5, min(2.0, profile.rate * speed)))
        rate = engine_speed(step)  # actual speed vs Heami's default, for alignment estimates
        ssml, words, spans = build_ssml(chunks, pitch_st=profile.pitch_st)
        if not words:
            raise UpstreamError("nothing to synthesise", retryable=False)
        out, ssml_path = await asyncio.to_thread(_prepare, Path(out_wav), ssml)
        voice = profile.provider_voice("sapi") or self.voice_name
        try:
            res = await run(_ps_args(self.exe, "-Mode", "speak", "-InFile", str(ssml_path), "-OutWav", str(out),
                                     "-Voice", voice, "-Rate", str(SAPI_SAMPLE_RATE), "-Speed", str(step)),
                            time_limit=self.time_limit)
        except MediaError as exc:
            raise UpstreamError(f"SAPI synthesis failed: {exc.stderr_tail[-300:] or exc}", retryable=False) from exc
        finally:
            await asyncio.to_thread(ssml_path.unlink, missing_ok=True)
        duration = await asyncio.to_thread(_wav_duration, out)
        if duration <= 0.05 or math.isnan(duration):
            raise UpstreamError("SAPI produced no audio", retryable=False)
        events = parse_progress(res.stdout.decode("utf-8", errors="replace"))
        timings = align_words(words, spans, events, duration, rate=rate)
        return TTSResult(wav_path=out, words=timings, duration_s=round(duration, 4), voice_id=voice_id,
                         provider=self.provider,
                         usage={"krw": 0.0, "chars": sum(len(w) for w in words), "events": len(events),
                                "engine_voice": voice, "engine_rate": step, "rate": round(rate, 3),
                                "pitch_st": profile.pitch_st})


def _prepare(out_wav: Path, ssml: str) -> tuple[Path, Path]:
    out = out_wav.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    ssml_path = out.with_suffix(".ssml.xml")
    ssml_path.write_text(ssml, encoding="utf-8")
    return out, ssml_path


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as fh:
        return fh.getnframes() / float(fh.getframerate() or SAPI_SAMPLE_RATE)
