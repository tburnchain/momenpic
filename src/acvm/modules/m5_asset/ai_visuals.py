"""AI scene media for *scene* beats (problem/amplify) — automatic as soon as GPT Image (and Higgsfield) keys are active.

Rules (§4 M5, §8.2, AI 기본법 표시):

* only beats whose visual is a **scene** (never product beats — the product is shown only from real product
  imagery; never text cards); a beat the operator assigned in the Studio is left alone;
* own footage and licensed stock still win: generation happens only where the planner would otherwise fall back to
  a generated card or a generic AI background;
* prompts forbid products, brands, readable text and recognisable faces; Higgsfield only animates the generated
  still (camera/ambient motion);
* every output is registered in the rights ledger as ``ai_generated_background`` (no product link, so policy forbids
  using it on a product beat), charged to the cost ledger, and labelled on screen by the renderer;
* idempotent per (content, beat, narration): re-renders reuse the media; editing the beat's narration regenerates;
* any provider failure or budget stop falls back to the existing card — rendering never fails because of AI media.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acvm import integrations
from acvm.core.clock import to_kst
from acvm.core.enums import AssetSource, ContentKind
from acvm.core.errors import ACVMError, BudgetExceeded
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.domain.models import Asset, Content, Product
from acvm.media.render_options import parse_render_options
from acvm.media.selection import ledger_candidates, plan_visuals, visual_class

if TYPE_CHECKING:
    from acvm.context import AppContext
    from acvm.modules.m5_asset.service import AssetService

log = get_logger(__name__)

STATE_TTL = timedelta(days=180)
SCENE_PROMPT = (
    "Vertical 9:16 photorealistic smartphone photo with natural light, used as the background of a Korean "
    "short-form video.\n"
    "Show this everyday situation from Korean daily life ({category}): \"{situation}\"\n"
    "Strict rules: show only the situation or problem itself. Do not show any product for sale, packaging, brand, "
    "logo, or any readable text, letters or numbers. No recognizable or famous people and no faces — at most hands "
    "or a person seen from behind. Keep the top 15% and the bottom 30% of the frame visually calm for captions."
)
CLIP_PROMPT = ("Subtle natural ambient motion with a slow, smooth camera push-in. Keep the scene exactly as it is: "
               "no new objects, people, faces, text or logos, no scene cut.")
CLIP_NEGATIVE = ("text, letters, watermark, logo, brand, product packaging, face, distortion, morphing, "
                 "extra limbs, flicker, scene cut")
_GENERIC = {AssetSource.AI_GENERATED_BACKGROUND, AssetSource.GENERATED_CARD}


def scene_prompt(narration: str, category: str) -> str:
    situation = " ".join(narration.split())[:400].replace('"', "'")
    return SCENE_PROMPT.format(category=category or "생활", situation=situation)


class AIVisualService:
    def __init__(self, ctx: AppContext, assets: AssetService) -> None:
        self.ctx = ctx
        self.assets = assets

    def _state_key(self, content_id: uuid.UUID) -> str:
        return f"ai_visuals:{content_id}"

    async def state(self, content_id: uuid.UUID) -> dict[str, Any]:
        async with self.ctx.uow() as repos:
            return await repos.kv.get_stale(self._state_key(content_id)) or {}

    async def _save_state(self, content_id: uuid.UUID, st: dict[str, Any]) -> None:
        async with self.ctx.uow() as repos:
            await repos.kv.put(self._state_key(content_id), st, STATE_TTL, self.ctx.clock.now())

    async def _affordable(self, workspace_id: uuid.UUID, amount: float) -> bool:
        async with self.ctx.uow() as repos:
            spent = await repos.costs.workspace_total(workspace_id, self.ctx.day_start_kst())
            budget = await self.ctx.daily_budget(repos, workspace_id)
        return spent + amount <= budget + 1e-9

    async def _charge(self, content: Content, stage: str, amount: float, key: str, detail: dict[str, Any]) -> None:
        async with self.ctx.uow() as repos:
            await self.ctx.charge(repos, workspace_id=content.workspace_id, run_id=content.created_by_run,
                                  stage=stage, amount_krw=amount, key=key, content_id=content.id, detail=detail)

    async def _alive(self, asset_id: str | None) -> Asset | None:
        if not asset_id:
            return None
        async with self.ctx.uow() as repos:
            return await repos.assets.get(uuid.UUID(asset_id))

    async def eligible_beats(self, content: Content, product: Product, state: dict[str, Any]) -> list[int]:
        """Scene beats where generated media would replace a card/generic background, operator choices untouched."""
        beats = content.script.beats
        opts = parse_render_options(content.render_options, n_beats=len(beats))
        assigned = {i: str(a) for i, a in opts.beat_asset_ids().items()}
        ours = {int(k): v for k, v in (state.get("beats") or {}).items()}
        today = to_kst(self.ctx.clock.now()).date()
        async with self.ctx.uow() as repos:
            candidates = await ledger_candidates(repos, product)
        plan = plan_visuals(beats, candidates, product=product, today=today, seed=str(content.id),
                            hook=content.script.hook)
        out: list[int] = []
        for choice in plan:
            i = choice.beat_index
            if visual_class(choice.visual) != "scene":
                continue
            if i in assigned and assigned[i] not in (ours.get(i) or {}).get("assets", []):
                continue  # the operator picked this beat's source
            if choice.asset is not None and choice.asset.rights_source not in _GENERIC and i not in assigned:
                continue  # own footage / licensed stock beats generation
            out.append(i)
        return out

    async def enrich(self, content: Content, product: Product) -> Content:
        """Generate/attach AI scene media where eligible; returns the (possibly updated) content."""
        if content.kind is not ContentKind.SHORT or not content.script.beats:
            return content
        image_gen = await integrations.image_gen(self.ctx)
        if image_gen is None:
            return content
        policy = self.ctx.settings.ai_visuals
        video_gen = await integrations.video_gen(self.ctx) if policy.animate_scenes else None
        state = await self.state(content.id)
        beats_state: dict[str, Any] = dict(state.get("beats") or {})
        try:
            eligible = (await self.eligible_beats(content, product, state))[:max(0, policy.max_images_per_content)]
        except ACVMError as exc:
            log.warning("ai_visuals_plan_failed", content_id=str(content.id), error=str(exc)[:160])
            return content
        if not eligible:
            return content
        category = product.category_name or product.category_id
        assigned: dict[int, str] = {}
        clips = 0
        errors: list[str] = []
        for i in eligible:
            beat = content.script.beats[i]
            nh = stable_hash({"narration": beat.narration, "category": category}, length=12)
            prev = beats_state.get(str(i)) or {}
            if prev.get("narration_hash") == nh:
                chosen = await self._alive(prev.get("video")) or await self._alive(prev.get("image"))
                if chosen is not None:
                    assigned[i] = str(chosen.id)
                    clips += 1 if chosen.kind == "video" else 0
                    continue
            try:
                entry = await self._generate_beat(content, i, beat.narration, category, nh, image_gen,
                                                  video_gen if clips < policy.max_clips_per_content else None)
            except BudgetExceeded as exc:
                errors.append(f"beat {i}: 예산 한도 — {exc}")
                break
            except Exception as exc:  # provider outage, refusal, bad output → keep the card
                log.warning("ai_visual_failed", content_id=str(content.id), beat=i, error=f"{type(exc).__name__}: "
                            f"{str(exc)[:160]}")
                errors.append(f"beat {i}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if entry is None:
                errors.append(f"beat {i}: 예산 부족으로 생략")
                continue
            beats_state[str(i)] = entry
            assigned[i] = entry.get("video") or entry["image"]
            clips += 1 if entry.get("video") else 0
        await self._save_state(content.id, {"beats": beats_state, "errors": errors,
                                            "at": self.ctx.clock.now().isoformat(),
                                            "providers": [image_gen.provider, *([video_gen.provider] if video_gen else [])]})
        if not assigned:
            return content
        return await self._assign(content, assigned)

    async def _generate_beat(self, content: Content, i: int, narration: str, category: str, nh: str,
                             image_gen: Any, video_gen: Any) -> dict[str, Any] | None:
        cfg = self.ctx.settings
        if not await self._affordable(content.workspace_id, cfg.openai_image.cost_per_image_krw):
            return None
        prompt = scene_prompt(narration, category)
        media = await image_gen.generate(prompt)
        still = await self.assets.ingest_bytes(
            media.data, kind="image", rights_source=AssetSource.AI_GENERATED_BACKGROUND,
            license=f"{media.provider}:{media.model} generated output — commercial use under the provider's terms",
            ext=".jpg" if media.mime == "image/jpeg" else ".png",
            tags=["ai_scene", f"ai:{media.provider}", f"content:{content.id}"],
            attribution=f"AI scene for beat {i} ({media.provider}/{media.model}, prompt {stable_hash(prompt, length=10)})")
        await self._charge(content, "ai.image", float(media.usage.get("krw", cfg.openai_image.cost_per_image_krw)),
                           f"{content.id}:ai.image:{i}:{nh}", {"provider": media.provider, "model": media.model,
                                                                "asset_id": str(still.id), "beat": i})
        entry: dict[str, Any] = {"narration_hash": nh, "image": str(still.id), "assets": [str(still.id)],
                                 "provider": media.provider}
        if video_gen is not None and await self._affordable(content.workspace_id, cfg.higgsfield.cost_per_clip_krw):
            try:
                clip = await video_gen.image_to_video(media.data, media.mime, CLIP_PROMPT,
                                                      negative_prompt=CLIP_NEGATIVE,
                                                      idempotency_key=f"acvm-{content.id}-{i}-{nh}")
                with tempfile.TemporaryDirectory(prefix="acvm_aiclip_", ignore_cleanup_errors=True) as td:
                    path = Path(td) / "clip.mp4"
                    path.write_bytes(clip.data)
                    video = await self.assets.derive_asset(still, path, "video", op=f"{clip.provider}_i2v")
                await self._charge(content, "ai.video", float(clip.usage.get("krw", cfg.higgsfield.cost_per_clip_krw)),
                                   f"{content.id}:ai.video:{i}:{nh}", {"provider": clip.provider, "model": clip.model,
                                                                       "asset_id": str(video.id), "beat": i})
                entry.update(video=str(video.id), assets=[str(still.id), str(video.id)], video_provider=clip.provider)
            except BudgetExceeded:
                raise
            except Exception as exc:  # the still is still a better scene than a card
                log.warning("ai_clip_failed", content_id=str(content.id), beat=i,
                            error=f"{type(exc).__name__}: {str(exc)[:160]}")
                entry["video_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        log.info("ai_scene_ready", content_id=str(content.id), beat=i, image=entry["image"], video=entry.get("video"))
        return entry

    async def _assign(self, content: Content, assigned: dict[int, str]) -> Content:
        async with self.ctx.uow() as repos:
            fresh = await repos.contents.get(content.id) or content
            opts = dict(fresh.render_options or {})
            beat_assets = {str(k): str(v) for k, v in (opts.get("beat_assets") or {}).items()}
            for i, aid in assigned.items():
                beat_assets[str(i)] = aid
            opts["beat_assets"] = beat_assets
            if opts != (fresh.render_options or {}):
                fresh = fresh.model_copy(update={"render_options": opts})
                await repos.contents.save(fresh)
        return fresh
