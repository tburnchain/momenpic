"""AI integrations: provider clients (Gemini · GPT Image · Higgsfield · ElevenLabs), the LLM chain, key registration
→ verification → activation without restart, and the automatic AI scene step (scene beats only, ledger, cost, label).

All HTTP is mocked (httpx.MockTransport); nothing here touches the network.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel

from acvm import integrations as ai
from acvm.cli import NICHES, seed_workspace
from acvm.config import GeminiConfig, HiggsfieldConfig, OpenAIImageConfig
from acvm.context import AppContext, resolve_llm
from acvm.core.enums import AssetSource, ContentKind, HookType, Platform
from acvm.core.errors import PolicyViolation, UpstreamError
from acvm.domain.models import Content, Product, Script, ScriptBeat, Workspace
from acvm.infra.gemini import GeminiLLM, GeminiSTT, check_gemini, inline_refs
from acvm.infra.higgsfield import HiggsfieldVideoGen, check_higgsfield
from acvm.infra.llm import AnthropicLLM, ChainLLM, LLMRefusal, LLMTruncated
from acvm.infra.openai_image import OpenAIImageGen, check_openai_image
from acvm.media.elevenlabs_voices import match_voices
from acvm.media.voices import VOICE_CATALOG
from acvm.modules.m5_asset.service import AssetService
from acvm.ports import GeneratedMedia

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_s: float) -> None:
    return None


def _jpeg(color: tuple[int, int, int] = (40, 90, 140), size: tuple[int, int] = (64, 96)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


# ───────────────────────── Gemini ─────────────────────────


class Verdict(BaseModel):
    class Item(BaseModel):
        name: str
        ok: bool

    items: list[Item]
    note: str = ""


def _gemini_ok(text: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]},
                                                     "finishReason": "STOP"}],
                                     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}, **extra})


async def test_gemini_structured_with_vision_inlines_schema_and_records_usage() -> None:
    seen: list[dict[str, Any]] = []
    usage: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-goog-api-key"] == "g-key" and "key=" not in str(req.url)
        assert req.url.path.endswith("/models/gemini-3.8-flash:generateContent")
        seen.append(json.loads(req.content))
        return _gemini_ok('{"items": [{"name": "a", "ok": true}], "note": "x"}')

    async def sink(u: Any) -> None:
        usage.append(u)

    llm = GeminiLLM(GeminiConfig(), api_key="g-key", http=_client(handler), usage_sink=sink, sleep=_no_sleep)
    out = await llm.structured(tier="medium", system="sys", user="check", schema=Verdict, images=[_jpeg()],
                               purpose="gate_r03")
    assert out.items[0].ok is True and out.note == "x"
    body = seen[0]
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg" and parts[-1]["text"] == "check"
    schema = body["generationConfig"]["responseJsonSchema"]
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert usage and usage[0].input_tokens == 10 and usage[0].cost_krw > 0


async def test_gemini_retries_falls_back_on_schema_400_and_maps_finish_reasons() -> None:
    calls: list[dict[str, Any]] = []
    script = [httpx.Response(429, json={"error": {"message": "slow down"}}),
              httpx.Response(400, json={"error": {"message": "schema not supported"}}),
              _gemini_ok('{"items": []}')]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return script.pop(0)

    llm = GeminiLLM(GeminiConfig(), api_key="k", http=_client(handler), sleep=_no_sleep)
    out = await llm.structured(tier="small", system="s", user="u", schema=Verdict)
    assert out.items == [] and len(calls) == 3
    assert "responseJsonSchema" not in calls[2]["generationConfig"] and "JSON 스키마" in calls[2]["contents"][0]["parts"][-1]["text"]

    for reason, exc in (("SAFETY", LLMRefusal), ("MAX_TOKENS", LLMTruncated)):
        llm = GeminiLLM(GeminiConfig(), api_key="k", sleep=_no_sleep, http=_client(
            lambda _r, reason=reason: httpx.Response(200, json={"candidates": [{"finishReason": reason}]})))
        with pytest.raises(exc):
            await llm.text(tier="small", system="s", user="u")
    bad = GeminiLLM(GeminiConfig(), api_key="k", sleep=_no_sleep,
                    http=_client(lambda _r: httpx.Response(403, json={"error": {"message": "denied"}})))
    with pytest.raises(UpstreamError) as ei:
        await bad.text(tier="small", system="s", user="u")
    assert ei.value.retryable is False


async def test_gemini_check_and_stt() -> None:
    cfg = GeminiConfig()
    ok = await check_gemini(cfg, "k", http=_client(lambda r: httpx.Response(200, json={"displayName": "Gemini Flash"})))
    assert ok["ok"] and "Gemini Flash" in ok["detail"]
    assert not (await check_gemini(cfg, "k", http=_client(lambda r: httpx.Response(400))))["ok"]
    missing = await check_gemini(cfg, "k", http=_client(lambda r: httpx.Response(404)))
    assert not missing["ok"] and "모델" in missing["detail"]

    def stt(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "audio/wav"
        return _gemini_ok(json.dumps({"text": "안녕하세요 반가워요", "segments": [
            {"text": "안녕하세요 반가워요", "start": 0.5, "end": 2.5}]}))

    engine = GeminiSTT(GeminiLLM(cfg, api_key="k", http=_client(stt), sleep=_no_sleep))
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        wav.write_bytes(b"RIFF0000WAVEfmt ")
        tr = await engine.transcribe_wav(wav, language="ko")
    assert tr.engine == "gemini" and tr.text == "안녕하세요 반가워요"
    assert [w.text for w in tr.words] == ["안녕하세요", "반가워요"] and tr.words[0].start == 0.5
    assert tr.words[-1].end == pytest.approx(2.5)


def test_inline_refs_resolves_nested_models() -> None:
    out = inline_refs(Verdict.model_json_schema())
    assert out["properties"]["items"]["items"]["properties"]["ok"]["type"] == "boolean"


# ───────────────────────── LLM chain ─────────────────────────


class _Member:
    def __init__(self, provider: str, exc: Exception | None = None) -> None:
        self.provider = provider
        self.exc = exc
        self.calls = 0

    async def text(self, **_kw: Any) -> str:
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.provider

    async def structured(self, **_kw: Any) -> Any:
        return await self.text()


async def test_chain_falls_back_on_outage_but_never_on_refusal() -> None:
    down, gem = _Member("anthropic", UpstreamError("down", status=529)), _Member("gemini")
    chain = ChainLLM([down, gem])
    assert chain.provider == "anthropic+gemini"
    assert await chain.text(tier="small", system="s", user="u") == "gemini"
    refusing = ChainLLM([_Member("anthropic", LLMRefusal("no")), gem2 := _Member("gemini")])
    with pytest.raises(LLMRefusal):
        await refusing.text(tier="small", system="s", user="u")
    assert gem2.calls == 0


async def test_resolve_llm_builds_the_chain_from_keys(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert await resolve_llm(ctx.settings, ctx.secrets) is None
    await ctx.secrets.put("gemini/api_key", "g-key-123456")
    only = await resolve_llm(ctx.settings, ctx.secrets)
    assert isinstance(only, GeminiLLM)
    await ctx.secrets.put("anthropic/api_key", "sk-ant-test-000000")
    both = await resolve_llm(ctx.settings, ctx.secrets)
    assert isinstance(both, ChainLLM) and both.provider == "anthropic+gemini"
    assert isinstance(await resolve_llm(ctx.settings, ctx.secrets, disabled={"gemini"}), AnthropicLLM)
    assert isinstance(await resolve_llm(ctx.settings, ctx.secrets, disabled={"anthropic"}), GeminiLLM)


# ───────────────────────── OpenAI GPT Image ─────────────────────────


async def test_openai_image_generates_and_maps_errors() -> None:
    img = _jpeg()
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer sk-test" and req.url.path == "/v1/images/generations"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(img).decode()}],
                                         "usage": {"total_tokens": 100}})

    gen = OpenAIImageGen(OpenAIImageConfig(), api_key="sk-test", http=_client(handler), sleep=_no_sleep)
    media = await gen.generate("a calm kitchen")
    assert media.data == img and media.mime == "image/jpeg" and media.usage["krw"] == 90.0
    assert seen[0]["model"] == "gpt-image-2.5-flare" and seen[0]["size"] == "1024x1536" and seen[0]["n"] == 1
    refusal = OpenAIImageGen(OpenAIImageConfig(), api_key="k", sleep=_no_sleep, http=_client(
        lambda r: httpx.Response(400, json={"error": {"code": "moderation_blocked", "message": "no"}})))
    with pytest.raises(PolicyViolation):
        await refusal.generate("x")
    flaky = [httpx.Response(429), httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(img).decode()}]})]
    retry = OpenAIImageGen(OpenAIImageConfig(), api_key="k", sleep=_no_sleep, http=_client(lambda r: flaky.pop(0)))
    assert (await retry.generate("x")).data == img

    cfg = OpenAIImageConfig()
    assert (await check_openai_image(cfg, "k", http=_client(lambda r: httpx.Response(200, json={}))))["ok"]
    for code, word in ((401, "유효"), (403, "조직 인증"), (404, "모델")):
        res = await check_openai_image(cfg, "k", http=_client(lambda r, code=code: httpx.Response(code)))
        assert not res["ok"] and word in res["detail"]


# ───────────────────────── Higgsfield ─────────────────────────


def _higgsfield_handler(log: list[str], *, final: str = "completed", video: bytes = b"\x00\x00\x00 ftypisom") -> Handler:
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        log.append(f"{req.method} {req.url.path}")
        if url.startswith("https://upload.example"):
            assert "Authorization" not in req.headers  # presigned URL: no credentials
            return httpx.Response(200)
        if url.startswith("https://cdn.example"):
            return httpx.Response(200, content=video)
        assert req.headers["Authorization"] == "Key kid:ksecret"
        if req.url.path == "/files/generate-upload-url":
            assert json.loads(req.content) == {"content_type": "image/jpeg"}
            return httpx.Response(200, json={"public_url": "https://cdn.example/in.jpg",
                                             "upload_url": "https://upload.example/put", "content_type": "image/jpeg",
                                             "upload_headers": {"Content-Type": "image/jpeg"}})
        if req.url.path.endswith("/image-to-video"):
            body = json.loads(req.content)
            assert body["image_url"] == "https://cdn.example/in.jpg" and body["duration"] == 5
            assert req.headers["Idempotency-Key"] == "idem-1"
            return httpx.Response(200, json={"status": "queued", "request_id": "r1",
                                             "status_url": "https://api.higgsfield.ai/requests/r1/status"})
        if req.url.path == "/requests/r1/status":
            polls["n"] += 1
            if polls["n"] < 3:
                return httpx.Response(200, json={"status": "in_progress", "request_id": "r1"})
            return httpx.Response(200, json={"status": final, "request_id": "r1",
                                             "video": {"url": "https://cdn.example/out.mp4"}})
        return httpx.Response(404)
    return handler


async def test_higgsfield_upload_submit_poll_download() -> None:
    log: list[str] = []
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    gen = HiggsfieldVideoGen(HiggsfieldConfig(), key_id="kid", key_secret="ksecret",
                             http=_client(_higgsfield_handler(log)), sleep=sleep)
    media = await gen.image_to_video(_jpeg(), "image/jpeg", "slow push-in", negative_prompt="text",
                                     idempotency_key="idem-1")
    assert media.mime == "video/mp4" and media.data.endswith(b"ftypisom") and media.usage["request_id"] == "r1"
    assert log[:3] == ["POST /files/generate-upload-url", "PUT /put",
                       "POST /kling-video/v2.5-turbo/standard/image-to-video"]
    assert sleeps == [2.0, 3.0]  # 2 s, ×1.5 backoff

    nsfw = HiggsfieldVideoGen(HiggsfieldConfig(), key_id="kid", key_secret="ksecret", sleep=_no_sleep,
                              http=_client(_higgsfield_handler([], final="nsfw")))
    with pytest.raises(PolicyViolation):
        await nsfw.image_to_video(_jpeg(), "image/jpeg", "x", idempotency_key="idem-1")
    broke = HiggsfieldVideoGen(HiggsfieldConfig(), key_id="kid", key_secret="ksecret", sleep=_no_sleep,
                               http=_client(lambda r: httpx.Response(403, json={"detail": "Insufficient credits"})))
    with pytest.raises(UpstreamError) as ei:
        await broke.image_to_video(_jpeg(), "image/jpeg", "x")
    assert ei.value.retryable is False and "크레딧" in str(ei.value)

    cfg = HiggsfieldConfig()
    assert (await check_higgsfield(cfg, "a", "b", http=_client(lambda r: httpx.Response(404))))["ok"]
    assert not (await check_higgsfield(cfg, "a", "b", http=_client(lambda r: httpx.Response(401))))["ok"]


# ───────────────────────── ElevenLabs voice mapping ─────────────────────────


def test_elevenlabs_mapping_prefers_korean_fast_voices_and_skips_narrators() -> None:
    voices = [
        {"voice_id": "f-narr", "name": "Story", "labels": {"gender": "female", "age": "middle_aged",
                                                          "use_case": "narration", "description": "calm"}},
        {"voice_id": "f-ko", "name": "Jisoo", "labels": {"gender": "female", "age": "young",
                                                         "use_case": "social_media"},
         "verified_languages": [{"language": "ko"}]},
        {"voice_id": "f-asmr", "name": "Whisper", "labels": {"gender": "female", "use_case": "asmr"}},
        {"voice_id": "m-conv", "name": "Minho", "labels": {"gender": "male", "age": "young",
                                                           "use_case": "conversational"}},
        {"voice_id": "m-deep", "name": "Deep", "labels": {"gender": "male", "age": "old", "description": "deep calm"}},
        {"voice_id": "f-upbeat", "name": "Bora", "labels": {"gender": "female", "age": "middle_aged",
                                                            "description": "upbeat energetic"}},
    ]
    m = match_voices(voices)
    assert set(m) == {v.id for v in VOICE_CATALOG}
    assert m["ko-F-30s-brisk"]["voice_id"] == "f-ko"  # Korean-verified, young, social first
    assert m["ko-M-30s-brisk"]["voice_id"] == "m-conv"
    for vid, pick in m.items():
        gender = "female" if "-F-" in vid else "male"
        assert next(v for v in voices if v["voice_id"] == pick["voice_id"])["labels"]["gender"] == gender
    female_picks = [m[v.id]["voice_id"] for v in VOICE_CATALOG if v.gender == "F"]
    assert female_picks.index("f-narr") > female_picks.index("f-upbeat")  # narrators only once fresh voices run out
    assert match_voices([]) == {}


# ───────────────────────── console flow: register → verify → active ─────────────────────────


@pytest.fixture
async def api(ctx):  # type: ignore[no-untyped-def]
    from acvm.api.app import create_app
    from acvm.infra.platforms.registry import build_platform_clients

    for _, (handle, _) in NICHES.items():
        for p in Platform:
            await ctx.secrets.put(f"tokens/{p.value}/{handle}", "sandbox-token")
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    app = create_app(ctx.settings, ctx=ctx)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acvm.test") as c):
        c.headers["Authorization"] = f"Bearer {ctx.settings.security.dev_api_token}"
        yield c, ctx, ws_id, app
    ctx.llm = None


def _provider_http(*, bad: set[str] = frozenset(), voices: list[dict[str, Any]] | None = None) -> httpx.AsyncClient:  # type: ignore[assignment]
    """One mock for every provider's check endpoint; keys listed in ``bad`` are rejected."""

    def handler(req: httpx.Request) -> httpx.Response:
        key = (req.headers.get("x-goog-api-key") or req.headers.get("xi-api-key") or req.headers.get("x-api-key")
               or req.headers.get("Authorization", "").split(" ", 1)[-1])
        if key in bad:
            return httpx.Response(401, json={"detail": "Invalid credentials"})
        host = req.url.host
        if host == "api.higgsfield.ai":
            return httpx.Response(404)  # unknown request id ⇒ credentials accepted
        if host == "api.elevenlabs.io" and req.url.path == "/v2/voices":
            return httpx.Response(200, json={"voices": voices or [], "has_more": False})
        if host == "api.elevenlabs.io":
            return httpx.Response(200, json={"character_count": 1200, "character_limit": 100000})
        return httpx.Response(200, json={"displayName": "ok"})
    return _client(handler)


async def test_keys_switch_everything_on_without_restart(api, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _, _ = api
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    voices = [{"voice_id": f"v{i}", "name": f"V{i}", "labels": {"gender": g, "age": "young"}}
              for i, g in enumerate(["female"] * 5 + ["male"] * 4)]
    ctx.extras["integrations.http"] = _provider_http(bad={"gm-bad-key-000"}, voices=voices)
    items = {x["name"]: x for x in (await c.get("/api/v1/system/integrations")).json()["items"]}
    assert set(items) == {"anthropic", "gemini", "openai_image", "higgsfield", "elevenlabs"}
    assert all(x["state"] == "missing" and x["automates"] for x in items.values())
    status = (await c.get("/api/v1/system/status")).json()
    assert {s["name"] for s in status["secrets"]} >= {"gemini_api_key", "openai_api_key", "higgsfield_key_id",
                                                      "higgsfield_key_secret", "elevenlabs_api_key"}

    # Gemini alone → writer/critic run on Gemini immediately
    r = await c.put("/api/v1/system/secrets/gemini_api_key", json={"value": "gm-good-key-000"})
    body = r.json()
    assert r.status_code == 200 and "gm-good" not in r.text
    assert body["integration"]["state"] == "active" and getattr(ctx.llm, "provider", "") == "gemini"
    checks = {x["id"]: x for x in (await c.get("/api/v1/system/status")).json()["checks"]}
    assert checks["llm"]["status"] == "ok" and "Gemini" in checks["llm"]["detail"]

    # + Claude → chain with automatic Gemini stand-in; pausing Gemini leaves Claude alone
    await c.put("/api/v1/system/secrets/anthropic_api_key", json={"value": "sk-ant-test-000000"})
    assert ctx.llm.provider == "anthropic+gemini"
    paused = (await c.put("/api/v1/system/integrations/gemini", json={"enabled": False})).json()
    assert paused["state"] == "paused" and ctx.llm.provider == "anthropic"
    await c.put("/api/v1/system/integrations/gemini", json={"enabled": True})
    assert ctx.llm.provider == "anthropic+gemini"

    # GPT Image + Higgsfield (needs both id and secret)
    assert await ai.image_gen(ctx) is None
    await c.put("/api/v1/system/secrets/openai_api_key", json={"value": "sk-proj-test-0000"})
    assert (await ai.image_gen(ctx)).provider == "openai"
    half = (await c.put("/api/v1/system/secrets/higgsfield_key_id", json={"value": "hf-key-id-0000"})).json()
    assert half["integration"]["state"] == "missing" and await ai.video_gen(ctx) is None
    full = (await c.put("/api/v1/system/secrets/higgsfield_key_secret", json={"value": "hf-secret-0000"})).json()
    assert full["integration"]["state"] == "active" and (await ai.video_gen(ctx)).provider == "higgsfield"
    opts = (await c.get("/api/v1/studio/options")).json()["integrations"]
    assert opts["ai_scenes"] and opts["ai_motion"] and opts["llm"] == "anthropic+gemini"

    # ElevenLabs → TTS switches and the 9 fast voices are mapped from the account
    from acvm.media.tts import build_tts

    before = await build_tts(ctx)
    el = (await c.put("/api/v1/system/secrets/elevenlabs_api_key", json={"value": "el-key-0000000"})).json()
    assert el["integration"]["state"] == "active" and any("9종" in a for a in el["integration"]["actions"])
    assert len(el["integration"]["voices"]) == len(VOICE_CATALOG)
    vmap = json.loads(await ctx.secrets.get("elevenlabs/voice_map"))
    assert vmap["ko-F-30s-brisk"].startswith("v") and vmap["default"]
    after = await build_tts(ctx)
    assert after.provider == "elevenlabs" and after is not before

    # a bad key is reported and treated as inactive; deleting a key switches the feature off again
    bad = (await c.put("/api/v1/system/secrets/gemini_api_key", json={"value": "gm-bad-key-000"})).json()
    assert bad["integration"]["state"] == "error" and not await ai.is_active(ctx, "gemini")
    assert ctx.llm.provider == "anthropic"  # a key that failed verification is dropped from the chain
    gone = (await c.delete("/api/v1/system/secrets/openai_api_key")).json()
    assert gone["integration"]["state"] == "missing" and await ai.image_gen(ctx) is None
    assert (await c.delete("/api/v1/system/secrets/nope")).status_code == 404
    assert (await c.put("/api/v1/system/integrations/gemini", json={"enabled": False},
                        headers={"X-Dev-Role": "operator"})).status_code == 403
    audit = (await c.get("/api/v1/audit")).text if (await c.get("/api/v1/audit")).status_code == 200 else ""
    assert "gm-good-key" not in audit and "sk-proj" not in audit


# ───────────────────────── AI scene step (M5) ─────────────────────────


class FakeImageGen:
    provider = "openai"
    model = "gpt-image-test"

    def __init__(self, fail: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    async def generate(self, prompt: str, *, size: str | None = None) -> GeneratedMedia:
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        n = len(self.prompts)
        return GeneratedMedia(_jpeg((20 * n % 255, 90, 140), (1024, 1536)), "image/jpeg", self.provider, self.model,
                              prompt, {"krw": 90.0})


class FakeVideoGen:
    provider = "higgsfield"
    model = "kling-test"

    def __init__(self, mp4: bytes) -> None:
        self.mp4 = mp4
        self.keys: list[str] = []

    async def image_to_video(self, image: bytes, mime: str, prompt: str, *, seconds: int | None = None,
                             negative_prompt: str = "", idempotency_key: str = "") -> GeneratedMedia:
        assert mime == "image/jpeg" and "no new objects" in prompt and "logo" in negative_prompt
        self.keys.append(idempotency_key)
        return GeneratedMedia(self.mp4, "video/mp4", self.provider, self.model, prompt, {"krw": 450.0})


@pytest.fixture
async def clip(tmp_path: Path) -> bytes:
    from acvm.media.ffmpeg import run

    out = tmp_path / "clip.mp4"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=s=720x1280:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               str(out)])
    return out.read_bytes()


def _beats() -> list[ScriptBeat]:
    return [
        ScriptBeat(start_sec=0, end_sec=3, role="hook", narration="과자 봉지, 아직도 집게로 막아두세요?",
                   on_screen_text="과자 봉지 아직도 이렇게?", visual="hook_text"),
        ScriptBeat(start_sec=3, end_sec=7, role="amplify", narration="하루만 지나도 눅눅해져서 결국 버리게 되죠.",
                   visual="problem_scene"),
        ScriptBeat(start_sec=7, end_sec=11, role="reveal", narration="그래서 찾은 게 과자 밀봉 클립 20개입 세트예요.",
                   on_screen_text="과자 밀봉 클립 20개입", visual="product_closeup"),
        ScriptBeat(start_sec=11, end_sec=17, role="benefits", narration="리뷰가 3,812개 넘게 달린 상품이에요. "
                   "평점은 4.7점이에요. 로켓배송 상품이라 받기도 편해요.", on_screen_text="리뷰 3,812개 · 평점 4.7",
                   visual="benefit_card"),
        ScriptBeat(start_sec=17, end_sec=22, role="cta", narration="제품 정보는 프로필 링크에서 확인하세요.",
                   on_screen_text="제품 정보는 프로필 링크에서", visual="cta_card"),
    ]


async def _content(ctx: AppContext, ws: Workspace, product: Product) -> Content:
    script = Script(hooks=["과자 봉지, 아직도 집게로 막아두세요?"], hook_type=HookType.QUESTION, beats=_beats(),
                    body="", persona="30대 여성 직장인", template_id="problem-solution-v3", duration_sec=22)
    c = Content(workspace_id=ws.id, product_id=product.id, kind=ContentKind.SHORT, persona="30대 여성 직장인",
                template_id="problem-solution-v3", script=script, created_at=ctx.clock.now())
    async with ctx.uow() as r:
        await r.products.upsert(product)
        await r.contents.add(c)
    return c


async def test_ai_scene_step_generates_only_for_scene_beats(ctx: AppContext, workspace: Workspace, product_factory,  # type: ignore[no-untyped-def]
                                                            clip: bytes) -> None:
    product = product_factory()
    c = await _content(ctx, workspace, product)
    svc = AssetService(ctx)
    assert await svc.enrich_ai_visuals(c, product) == c  # no keys → nothing happens
    img, vid = FakeImageGen(), FakeVideoGen(clip)
    ctx.extras["image_gen"], ctx.extras["video_gen"] = img, vid

    c2 = await svc.enrich_ai_visuals(c, product)
    assert list(c2.render_options["beat_assets"]) == ["1"]  # the problem scene only — never product/text beats
    assert len(img.prompts) == 1 and "하루만 지나도" in img.prompts[0] and "Do not show any product" in img.prompts[0]
    assert "과자 밀봉 클립" not in img.prompts[0] and "No recognizable or famous people" in img.prompts[0]
    async with ctx.uow() as r:
        video = await r.assets.get(__import__("uuid").UUID(c2.render_options["beat_assets"]["1"]))
        assert video is not None and video.kind == "video"
        assert video.rights_source is AssetSource.AI_GENERATED_BACKGROUND and video.product_id is None
        assert any(t.startswith("derived:higgsfield_i2v:") for t in video.tags)
        assert await r.costs.content_total(c.id) == pytest.approx(90.0 + 450.0)
    assert vid.keys and vid.keys[0].startswith(f"acvm-{c.id}-1-")

    again = await svc.enrich_ai_visuals(c2, product)  # idempotent: re-render reuses the media
    assert again.render_options == c2.render_options and len(img.prompts) == 1 and len(vid.keys) == 1

    edited = again.model_copy(update={"script": again.script.model_copy(update={"beats": [
        b.model_copy(update={"narration": "봉지를 대충 접어두면 금방 눅눅해져요."}) if i == 1 else b
        for i, b in enumerate(again.script.beats)]})})
    async with ctx.uow() as r:
        await r.contents.save(edited)
    from acvm.modules.m5_asset.ai_visuals import AIVisualService

    first_image = (await AIVisualService(ctx, svc).state(c.id))["beats"]["1"]["image"]
    regenerated = await svc.enrich_ai_visuals(edited, product)  # narration edited → a new scene for the new text
    assert len(img.prompts) == 2 and "대충 접어두면" in img.prompts[1]
    assert (await AIVisualService(ctx, svc).state(c.id))["beats"]["1"]["image"] != first_image

    # the operator's own choice for a beat is never replaced
    own = await svc.ingest_bytes(_jpeg((200, 10, 10)), kind="image", rights_source=AssetSource.OWN_FOOTAGE,
                                 license="own shoot", ext=".jpg", tags=["studio"])
    chosen = regenerated.model_copy(update={"render_options": {"beat_assets": {"1": str(own.id)}}})
    async with ctx.uow() as r:
        await r.contents.save(chosen)
    kept = await svc.enrich_ai_visuals(chosen, product)
    assert kept.render_options["beat_assets"] == {"1": str(own.id)} and len(img.prompts) == 2


async def test_ai_scene_step_falls_back_on_failure_and_budget(ctx: AppContext, workspace: Workspace,  # type: ignore[no-untyped-def]
                                                              product_factory) -> None:
    product = product_factory()
    c = await _content(ctx, workspace, product)
    svc = AssetService(ctx)
    ctx.extras["image_gen"] = FakeImageGen(fail=UpstreamError("outage", status=503))
    assert (await svc.enrich_ai_visuals(c, product)).render_options == {}
    from acvm.modules.m5_asset.ai_visuals import AIVisualService

    st = await AIVisualService(ctx, svc).state(c.id)
    assert st["errors"] and "outage" in st["errors"][0]

    ctx.extras["image_gen"] = img = FakeImageGen()
    from acvm.db import tables as t

    async with ctx.uow() as r:  # today's budget below the price of one image → nothing is generated or charged
        row = await r.session.get(t.WorkspaceRow, workspace.id)
        assert row is not None
        row.daily_budget_krw = 10.0
    assert (await svc.enrich_ai_visuals(c, product)).render_options == {} and img.prompts == []
    async with ctx.uow() as r:
        assert await r.costs.content_total(c.id) == 0


@pytest.mark.render
async def test_render_labels_ai_scenes_on_screen(ctx: AppContext, workspace: Workspace, product_factory,  # type: ignore[no-untyped-def]
                                                 clip: bytes) -> None:
    from acvm.modules.m6_render import RenderService

    product = product_factory()
    c = await _content(ctx, workspace, product)
    ctx.extras["image_gen"], ctx.extras["video_gen"] = FakeImageGen(), FakeVideoGen(clip)
    await AssetService(ctx).enrich_ai_visuals(c, product)
    meta = await RenderService(ctx).render(c.id)
    tl = json.loads(await ctx.objects.get_bytes(meta.media_uri[: -len(".mp4")] + ".timeline.json"))
    assert [lab["scene"] for lab in tl["ai_labels"]] == [1] and tl["ai_labels"][0]["text"] == "AI 생성 장면"
    assert tl["scenes"][1]["kind"] == "video" and tl["scenes"][1]["rights_source"] == "ai_generated_background"
    assert all(s["rights_source"] != "ai_generated_background" for s in tl["scenes"] if s["visual"].startswith("product"))
    from acvm.media.ass import build_ass_document
    from acvm.media.safe_area import layout_from_dict

    doc = build_ass_document(width=1080, height=1920, font_family="T", layout=layout_from_dict(tl["layout"]),
                             overlay=tl["overlays"]["disclosure"], ai_labels=tl["ai_labels"])
    assert "AI 생성 장면" in doc and "\\an7" in doc


# ───────────────────────── Studio tools ─────────────────────────


async def test_studio_ai_motion_only_for_ai_scenes(api, clip: bytes) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _, _ = api
    svc = AssetService(ctx)
    scene = await svc.ingest_bytes(_jpeg((10, 120, 60)), kind="image", rights_source=AssetSource.AI_GENERATED_BACKGROUND,
                                   license="openai:gpt-image generated", ext=".jpg", tags=["ai_scene"])
    photo = await svc.ingest_bytes(_jpeg((120, 10, 60)), kind="image", rights_source=AssetSource.OWN_FOOTAGE,
                                   license="own shoot", ext=".jpg", tags=["studio"])
    r = await c.post(f"/api/v1/studio/assets/{scene.id}/i2v", json={"engine": "ai"})
    assert r.status_code == 409  # Higgsfield not registered
    ctx.extras["video_gen"] = FakeVideoGen(clip)
    assert (await c.post(f"/api/v1/studio/assets/{photo.id}/i2v", json={"engine": "ai"})).status_code == 403
    ok = await c.post(f"/api/v1/studio/assets/{scene.id}/i2v", json={"engine": "ai", "motion": "pan_left"})
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert out["kind"] == "video" and out["rights_source"] == "ai_generated_background"
    assert any(t.startswith("derived:higgsfield_i2v:") for t in out["tags"])
    camera = await c.post(f"/api/v1/studio/assets/{photo.id}/i2v", json={"engine": "camera"})
    assert camera.status_code == 200 and camera.json()["rights_source"] == "own_footage"
