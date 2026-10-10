"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader, DefList } from "@/components/ui/Card";
import { ErrorBanner, Notice } from "@/components/ui/Feedback";
import { usableImageUrl } from "@/components/ui/misc";
import { Icon } from "@/components/ui/Icon";
import { Pill, PublicationStatePill, PlatformTag, VerdictPill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { api, type DraftEdit } from "@/lib/api";
import { int, kst, krw } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { qk, studioKeys, useLiveLint, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { ClaimShape, DraftView, LintIssue, StudioAsset, StudioOptions, StudioPublishResult } from "@/lib/schemas";
import { PhonePreview, PublishResult, RenderFailed, RenderProgress, TargetChips, type PreviewModel, type TargetAccount } from "./PreviewPanel";
import { ScriptPanel, type LocalBeat } from "./ScriptPanel";
import { SourcePanel } from "./SourcePanel";
import { CaptionPanel, VoicePanel, WatermarkCard } from "./StylePanels";
import { ToolsMenu } from "./ToolsMenu";

type Group = "hooks" | "beats" | "body" | "caption" | "ro";

interface LocalRO {
  voice_id: string | null;
  caption_style: string | null;
  caption_position: string | null;
  captions_enabled: boolean;
  watermark: string;
  beat_assets: Record<string, string>;
}

interface Local {
  hooks: string[];
  chosen: number;
  beats: LocalBeat[];
  body: string;
  caption: string;
  ro: LocalRO;
}

type AssetRef = Pick<StudioAsset, "id" | "kind" | "url">;

function fromDraft(d: DraftView): Local {
  const ro = d.render_options;
  return {
    hooks: d.hooks.length ? [...d.hooks] : ["", "", ""],
    chosen: d.chosen_hook,
    beats: d.beats.map((b) => ({ role: b.role, start: b.start, end: b.end, narration: b.narration, on_screen_text: b.on_screen_text })),
    body: d.body,
    caption: d.caption,
    ro: {
      voice_id: ro.voice_id ?? null,
      caption_style: ro.caption_style ?? "classic",
      caption_position: ro.caption_position ?? "bottom",
      captions_enabled: ro.captions_enabled ?? true,
      watermark: ro.watermark ?? (d.account ? `@${d.account.handle.replace(/^@/, "")}` : ""),
      beat_assets: { ...(ro.beat_assets ?? {}) },
    },
  };
}

function buildBody(l: Local, dirty: Set<Group>): DraftEdit {
  const body: DraftEdit = {};
  if (dirty.has("hooks")) {
    body.hooks = l.hooks;
    body.chosen_hook = l.chosen;
  }
  if (dirty.has("beats")) body.beats = l.beats.map((b) => ({ narration: b.narration, on_screen_text: b.on_screen_text }));
  if (dirty.has("body")) body.body = l.body;
  if (dirty.has("caption")) body.caption = l.caption;
  if (dirty.has("ro")) body.render_options = { ...l.ro };
  return body;
}

/** Replace the flagged term (or drop the sentence) wherever the issue's sentence occurs. */
function applyFix(text: string, issue: LintIssue): string {
  if (!issue.sentence || !text.includes(issue.sentence)) return text;
  if (issue.suggestion && issue.suggestion !== "문장 삭제" && issue.term && issue.sentence.includes(issue.term)) {
    return text.replace(issue.sentence, issue.sentence.replace(issue.term, issue.suggestion));
  }
  return text
    .replace(issue.sentence, "")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/^[ \t]+|[ \t]+$/gm, "");
}

function mergeClaims(a: ClaimShape[] | undefined, b: ClaimShape[]): ClaimShape[] {
  const out: ClaimShape[] = [];
  for (const c of [...(a ?? []), ...b]) if (!out.some((x) => x.text === c.text)) out.push(c);
  return out;
}

export function StudioEditor({
  draft,
  options,
  targets,
  setTargets,
  targetAccounts,
  onNewVariant,
  newVariantPending,
}: {
  draft: DraftView;
  options: StudioOptions;
  targets: string[];
  setTargets: (fn: (t: string[]) => string[]) => void;
  targetAccounts: TargetAccount[];
  onNewVariant: () => void;
  newVariantPending: boolean;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { can, role } = usePrincipal();
  const locked = draft.status === "approved" || draft.status === "blocked";
  const canEdit = can("command") && !locked;
  const canAssets = can("manageAssets");

  const [local, setLocal] = useState<Local>(() => fromDraft(draft));
  const [dirty, setDirty] = useState<Set<Group>>(() => new Set());
  const [assetRefs, setAssetRefs] = useState<Record<string, AssetRef>>(() => {
    const m: Record<string, AssetRef> = {};
    draft.beats.forEach((b) => {
      if (b.asset) m[b.asset.id] = { id: b.asset.id, kind: b.asset.kind, url: b.asset.url };
    });
    return m;
  });
  const [renderSince, setRenderSince] = useState<number | null>(null);
  const [result, setResult] = useState<StudioPublishResult | null>(null);

  const { mutate: save, mutateAsync: saveAsync, isPending: saving, error: saveError } = useMutation({
    mutationFn: (body: DraftEdit) => api.updateDraft(draft.id, body),
    onSuccess: (view) => qc.setQueryData(studioKeys.draft(draft.id), view),
  });

  // Debounced autosave of the edited groups.
  useEffect(() => {
    if (!dirty.size || !canEdit) return;
    const body = buildBody(local, dirty);
    const t = setTimeout(() => {
      setDirty(new Set());
      save(body);
    }, 900);
    return () => clearTimeout(t);
  }, [dirty, local, canEdit, save]);

  const flushNow = async () => {
    if (!dirty.size || !canEdit) return;
    const body = buildBody(local, dirty);
    setDirty(new Set());
    await saveAsync(body);
  };

  const edit = (group: Group, fn: (l: Local) => Local) => {
    if (!canEdit) return;
    setLocal(fn);
    setDirty((d) => new Set(d).add(group));
  };

  /* ---------------------------------------------------------------- live lint */
  const lintText = useMemo(
    () => [...local.beats.map((b) => b.narration), ...local.beats.map((b) => b.on_screen_text), local.body, local.caption].join("\n"),
    [local],
  );
  const debouncedText = useDebounced(lintText, 400);
  // Shorts: the server also checks the 20–25 s window at the fast delivery rate (too_long blocks generation).
  const narrKey = useDebounced(draft.kind === "short" ? JSON.stringify(local.beats.map((b) => b.narration)) : "", 400);
  const narrations = useMemo(() => (narrKey ? (JSON.parse(narrKey) as string[]) : null), [narrKey]);
  const productId = draft.product?.id ?? null;
  const live = useLiveLint(productId, debouncedText, narrations);
  const issues = live.data?.issues ?? draft.lint.issues;
  const duration = (live.data ? live.data.duration : draft.lint.duration) ?? null;
  const errors = issues.filter((i) => i.severity === "error").length;
  // Live lint re-grounds the current text; fall back to the draft's stored claims until it answers.
  const claims = live.data ? mergeClaims(live.data.claims, draft.claims.filter((c) => c.kind === "cta" || c.kind === "opinion")) : draft.claims;

  const applyIssue = (issue: LintIssue) => {
    const inBeats = local.beats.some((b) => b.narration.includes(issue.sentence) || b.on_screen_text.includes(issue.sentence));
    const inHook = local.beats[0]?.role === "hook" && local.hooks[local.chosen]?.includes(issue.sentence);
    if (inHook) {
      edit("hooks", (l) => {
        const hooks = [...l.hooks];
        hooks[l.chosen] = applyFix(hooks[l.chosen], issue);
        const beats = l.beats.map((b, i) => (i === 0 ? { ...b, narration: hooks[l.chosen] } : b));
        return { ...l, hooks, beats };
      });
    }
    if (inBeats) {
      edit("beats", (l) => ({
        ...l,
        beats: l.beats.map((b) => ({ ...b, narration: applyFix(b.narration, issue), on_screen_text: applyFix(b.on_screen_text, issue) })),
      }));
    }
    if (local.body.includes(issue.sentence)) edit("body", (l) => ({ ...l, body: applyFix(l.body, issue) }));
    if (local.caption.includes(issue.sentence)) edit("caption", (l) => ({ ...l, caption: applyFix(l.caption, issue) }));
    toast("제안을 적용했습니다", "info");
  };

  /* ---------------------------------------------------------------- render / publish */
  const r = draft.render;
  const isRendering = r.state === "rendering";
  const hasOutput = r.state === "done" && !!(r.media_url || r.thumbnail_url);
  const staleRender = (r.state === "done" && !r.media_url && !r.thumbnail_url) || (hasOutput && dirty.size > 0);

  const render = useMutation({
    mutationFn: async () => {
      await flushNow();
      return api.renderDraft(draft.id);
    },
    onSuccess: () => {
      setRenderSince(Date.now());
      setResult(null);
      void qc.invalidateQueries({ queryKey: studioKeys.draft(draft.id) });
    },
  });

  const publish = useMutation({
    mutationFn: async () => {
      await flushNow();
      return api.publishDraft(draft.id, targets);
    },
    onSuccess: (res) => {
      setResult(res);
      void qc.invalidateQueries({ queryKey: studioKeys.draft(draft.id) });
      void qc.invalidateQueries({ queryKey: qk.publicationsAll });
      void qc.invalidateQueries({ queryKey: qk.holds });
      toast(res.verdict === "PASS" ? "게이트 통과 — 예약 게시했습니다" : `게이트 판정: ${res.verdict}`, res.verdict === "PASS" ? "good" : "info");
    },
  });

  /* ---------------------------------------------------------------- preview model */
  const firstAssetId = local.ro.beat_assets["0"];
  const firstAsset = firstAssetId ? (assetRefs[firstAssetId] ?? draft.beats[0]?.asset ?? null) : null;
  const preview: PreviewModel = {
    kind: draft.kind,
    backgroundUrl: firstAsset?.url ?? usableImageUrl(draft.product?.image_url),
    backgroundIsVideo: firstAsset?.kind === "video" && !!firstAsset.url,
    disclosure: options.disclosure.text,
    disclosureSeconds: options.disclosure.seconds ?? 3,
    caption: local.beats[0]?.on_screen_text || local.hooks[local.chosen] || "",
    captionStyle: local.ro.caption_style,
    captionPosition: local.ro.caption_position,
    captionsEnabled: local.ro.captions_enabled,
    watermark: local.ro.watermark,
    body: local.body,
    handle: draft.account ? `@${draft.account.handle.replace(/^@/, "")}` : "@account",
    mediaUrl: hasOutput && dirty.size === 0 ? (r.media_url ?? null) : null,
    posterUrl: hasOutput && dirty.size === 0 ? (r.thumbnail_url ?? null) : null,
    rendered: hasOutput && dirty.size === 0,
  };

  const saveLabel = saving ? "저장 중…" : dirty.size ? "변경됨 · 저장 대기" : saveError ? "저장 실패" : "저장됨";
  const blockReason = errors ? `검수 오류 ${errors}건을 먼저 고치세요` : null;
  const canPublishTargets = targetAccounts.length > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold text-ink">{draft.product?.name ?? "초안"}</span>
          <Pill tone="neutral">{draft.kind === "short" ? "쇼츠" : "글"}</Pill>
          {draft.persona ? <Pill tone="neutral">{draft.persona}</Pill> : null}
          {draft.gate ? <VerdictPill verdict={draft.gate.verdict} /> : null}
          <span className={`inline-flex items-center gap-1 text-xs ${saveError ? "text-critical-ink" : "text-muted"}`} aria-live="polite">
            <Icon name={saving ? "clock" : saveError ? "alert" : "check"} size={12} />
            {saveLabel}
          </span>
          {saveError ? (
            <button type="button" className="text-xs text-accent-ink hover:underline" onClick={() => setDirty(new Set<Group>(["hooks", "beats", "body", "caption", "ro"]))}>
              다시 저장
            </button>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Link href={`/contents/${encodeURIComponent(draft.id)}`} className="text-[13px] text-accent-ink hover:underline">
            콘텐츠 상세
          </Link>
          <ToolsMenu
            productId={productId}
            disabled={!canAssets}
            onUseTranscript={
              canEdit && draft.kind === "short" && local.beats.length > 1
                ? (t) => edit("beats", (l) => ({ ...l, beats: l.beats.map((b, i) => (i === 1 ? { ...b, narration: t.slice(0, 400) } : b)) }))
                : undefined
            }
          />
        </div>
      </div>

      {locked ? (
        <Notice tone="warn" title={draft.status === "approved" ? "승인된 콘텐츠" : "차단된 콘텐츠"}>
          승인·차단된 콘텐츠는 수정할 수 없습니다 — 같은 상품으로 새 초안을 만드세요.{" "}
          {can("command") ? (
            <button type="button" className="font-medium text-accent-ink hover:underline" onClick={onNewVariant}>
              새 변형 만들기
            </button>
          ) : null}
        </Notice>
      ) : !can("command") ? (
        <Notice>스튜디오 편집은 {ALLOWED_ROLES.command}만 할 수 있습니다{role ? ` (현재 역할: ${role})` : ""}. 초안은 열람만 가능합니다.</Notice>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_300px_minmax(0,1fr)]">
        {/* Left: ① ② ③ */}
        <div className="min-w-0 space-y-5">
          {draft.kind === "short" ? (
            <SourcePanel
              beats={local.beats.map((b, i) => {
                const id = local.ro.beat_assets[String(i)];
                return { role: b.role, start: b.start, end: b.end, asset: id ? (assetRefs[id] ?? draft.beats[i]?.asset ?? null) : null };
              })}
              overrides={local.ro.beat_assets}
              limit={options.limits.sources}
              productId={productId}
              readOnly={!canEdit}
              canUpload={canAssets}
              aiNote={
                options.integrations?.ai_scenes
                  ? `AI 장면 자동 생성 켜짐 — 소스를 지정하지 않은 문제 장면 비트는 렌더할 때 GPT Image 배경${
                      options.integrations.ai_motion ? " + Higgsfield 모션" : ""
                    }으로 채웁니다. 상품 비트는 실제 상품 사진만 씁니다.`
                  : null
              }
              aiErrors={draft.ai_visuals?.errors ?? []}
              onAssign={(i, a) => {
                if (a) setAssetRefs((m) => ({ ...m, [a.id]: { id: a.id, kind: a.kind, url: a.url } }));
                edit("ro", (l) => {
                  const map = { ...l.ro.beat_assets };
                  if (a) map[String(i)] = a.id;
                  else delete map[String(i)];
                  return { ...l, ro: { ...l.ro, beat_assets: map } };
                });
              }}
            />
          ) : null}
          <ScriptPanel
            kind={draft.kind}
            hookType={draft.hook_type}
            hooks={local.hooks}
            chosen={local.chosen}
            beats={local.beats}
            body={local.body}
            issues={issues}
            claims={claims}
            limitChars={options.limits.script_chars}
            minSeconds={options.limits.min_seconds}
            maxSeconds={options.limits.max_seconds}
            speechRate={options.limits.syllables_per_second}
            duration={duration}
            readOnly={!canEdit}
            linting={live.isFetching}
            onHook={(i, v) =>
              edit("hooks", (l) => {
                const hooks = l.hooks.map((h, k) => (k === i ? v : h));
                const beats = i === l.chosen && l.beats[0]?.role === "hook" ? l.beats.map((b, k) => (k === 0 ? { ...b, narration: v } : b)) : l.beats;
                return { ...l, hooks, beats };
              })
            }
            onChoose={(i) =>
              edit("hooks", (l) => ({
                ...l,
                chosen: i,
                beats: l.beats[0]?.role === "hook" ? l.beats.map((b, k) => (k === 0 ? { ...b, narration: l.hooks[i] } : b)) : l.beats,
              }))
            }
            onBeat={(i, field, v) => edit("beats", (l) => ({ ...l, beats: l.beats.map((b, k) => (k === i ? { ...b, [field]: v } : b)) }))}
            onBody={(v) => edit("body", (l) => ({ ...l, body: v }))}
            onApply={applyIssue}
          />
          {draft.kind === "short" ? (
            <WatermarkCard
              value={local.ro.watermark}
              max={options.limits.watermark_chars}
              placeholder={draft.account ? `@${draft.account.handle.replace(/^@/, "")}` : "@채널명"}
              readOnly={!canEdit}
              onChange={(v) => edit("ro", (l) => ({ ...l, ro: { ...l.ro, watermark: v } }))}
            />
          ) : null}
        </div>

        {/* Centre: preview + actions */}
        <div className="min-w-0 space-y-4">
          <PhonePreview m={preview} />
          <Card>
            <CardBody className="space-y-3 pt-4">
              {draft.kind === "short" ? (
                <>
                  {isRendering ? <RenderProgress since={renderSince} ai={!!options.integrations?.ai_scenes} /> : null}
                  {r.state === "failed" ? <RenderFailed error={r.error} /> : null}
                  {staleRender && !isRendering ? (
                    <p className="flex items-center gap-1.5 text-xs font-medium text-warn-ink">
                      <Icon name="alert" size={13} /> 변경됨 · 다시 생성 필요
                    </p>
                  ) : null}
                  {hasOutput && !staleRender ? (
                    <p className="text-xs text-muted">
                      렌더 완료{r.duration ? ` · ${Math.round(r.duration)}초` : ""}
                      {r.loudness_lufs !== null && r.loudness_lufs !== undefined ? ` · ${r.loudness_lufs} LUFS` : ""}
                      {r.voice_id ? ` · ${r.voice_id}` : ""}
                    </p>
                  ) : null}
                  <Button
                    variant={hasOutput && !staleRender ? "secondary" : "primary"}
                    size="lg"
                    icon="film"
                    className="w-full"
                    loading={render.isPending || isRendering}
                    disabled={!canEdit || !!blockReason}
                    onClick={() => render.mutate()}
                  >
                    {hasOutput && !staleRender ? "다시 생성하기" : "영상 생성하기"}
                  </Button>
                  {render.error ? <ErrorBanner error={render.error} compact /> : null}
                </>
              ) : null}

              <div className="space-y-2 border-t border-line pt-3">
                <p className="text-[13px] font-semibold text-ink-2">게시 대상</p>
                {canPublishTargets ? (
                  <TargetChips
                    accounts={targetAccounts}
                    selected={targets}
                    disabled={!canEdit}
                    onToggle={(id) => setTargets((t) => (t.includes(id) ? t.filter((x) => x !== id) : [...t, id]))}
                  />
                ) : (
                  <p className="text-xs text-muted">이 형식으로 게시할 수 있는 활성 계정이 없습니다.</p>
                )}
                <p className="text-[11px] text-muted">
                  {targets.length ? `${targets.length}개 계정` : `선택 안 하면 초안 계정${draft.account ? ` (${draft.account.handle})` : ""}`} · 게이트(M7) 통과 시 시간대 분산 예약
                </p>
                <Button
                  variant="success"
                  size="lg"
                  icon="shield"
                  className="w-full"
                  loading={publish.isPending}
                  disabled={!canEdit || !!blockReason || isRendering}
                  onClick={() => publish.mutate()}
                >
                  검수 후 게시
                </Button>
                {publish.isPending ? <p className="text-xs text-muted">렌더 → 게이트 → 예약까지 최대 30초 정도 걸립니다…</p> : null}
                {blockReason ? (
                  <p className="flex items-center gap-1.5 text-xs font-medium text-critical-ink">
                    <Icon name="alert" size={13} /> {blockReason}
                  </p>
                ) : null}
                {publish.error ? <ErrorBanner error={publish.error} compact /> : null}
                {result ? <PublishResult result={result} onNewVariant={onNewVariant} newVariantPending={newVariantPending} contentId={draft.id} /> : null}
              </div>
            </CardBody>
          </Card>
          {draft.publications.length && !result ? (
            <Card>
              <CardHeader title={`예약·게시 (${draft.publications.length})`} />
              <CardBody>
                <ul className="space-y-1.5">
                  {draft.publications.map((p) => (
                    <li key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
                      <PlatformTag platform={p.platform} className="text-[13px]" />
                      <PublicationStatePill state={p.state} />
                      <span className="text-xs tabular-nums text-muted">{kst(p.scheduled_at)}</span>
                    </li>
                  ))}
                </ul>
              </CardBody>
            </Card>
          ) : null}
        </div>

        {/* Right: ④ ⑤ */}
        <div className="min-w-0 space-y-5 lg:col-span-2 xl:col-span-1">
          {draft.kind === "short" ? (
            <>
              <VoicePanel options={options} persona={draft.persona} value={local.ro.voice_id} readOnly={!canEdit} onChange={(id) => edit("ro", (l) => ({ ...l, ro: { ...l.ro, voice_id: id } }))} />
              <CaptionPanel
                options={options}
                style={local.ro.caption_style}
                position={local.ro.caption_position}
                enabled={local.ro.captions_enabled}
                readOnly={!canEdit}
                onStyle={(id) => edit("ro", (l) => ({ ...l, ro: { ...l.ro, caption_style: id } }))}
                onPosition={(p) => edit("ro", (l) => ({ ...l, ro: { ...l.ro, caption_position: p } }))}
                onEnabled={(v) => edit("ro", (l) => ({ ...l, ro: { ...l.ro, captions_enabled: v } }))}
              />
            </>
          ) : (
            <Card>
              <CardHeader title="상품 팩트" description="본문의 사실 문장은 이 값으로만 근거를 댈 수 있습니다" />
              <CardBody>
                <DefList
                  items={[
                    ["가격", krw(draft.product?.price)],
                    ["가격 확인", kst(draft.product?.price_checked_at)],
                    ["평점", draft.product?.rating ?? "—"],
                    ["리뷰수", int(draft.product?.review_count)],
                    ["로켓배송", draft.product?.is_rocket ? "예" : "아니오"],
                  ]}
                />
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
