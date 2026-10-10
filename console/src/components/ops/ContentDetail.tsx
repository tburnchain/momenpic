"use client";

import Link from "next/link";
import { FindingsList, MediaPreview } from "@/components/runs/ContentCard";
import { CaptionPreview } from "@/components/ui/CaptionPreview";
import { Card, CardBody, CardHeader, DefList } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock, Skeleton } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { StatTile, Thumb } from "@/components/ui/misc";
import { Pill, PlatformTag, PublicationStatePill, VerdictPill } from "@/components/ui/Pill";
import { kst, krw, krwPrecise, pct, shortId } from "@/lib/format";
import { KIND_LABEL } from "@/lib/labels";
import { useContent } from "@/lib/queries";
import type { ContentDossier } from "@/lib/schemas";

const ROLE_LABEL: Record<string, string> = {
  hook: "훅",
  problem: "문제 제기",
  amplify: "불편 증폭",
  product: "상품 등장",
  benefits: "장점",
  demo: "사용 장면",
  cta: "CTA",
};

const STAGE_LABEL: Record<string, string> = { gate: "M7 게이트", preflight: "게시 직전 재검사", human: "사람 검토" };

const HOOK_TYPE: Record<string, string> = {
  question: "질문",
  shock: "충격",
  before_after: "전후비교",
  price_twist: "가격반전",
  empathy: "공감",
};

function fmtSec(s: number) {
  return `${Number.isInteger(s) ? s : s.toFixed(1)}초`;
}

function ScriptCard({ d }: { d: ContentDossier }) {
  const sc = d.script;
  const claimText = new Map(d.claims.map((c) => [c.id, c]));
  return (
    <Card aria-labelledby="ct-script">
      <CardHeader
        id="ct-script"
        title={d.kind === "short" ? "대본 (비트별)" : "본문"}
        description={`${sc.hook_type ? `${HOOK_TYPE[sc.hook_type] ?? sc.hook_type} 훅 · ` : ""}${sc.duration_sec ? `${fmtSec(sc.duration_sec)} · ` : ""}${sc.persona ?? d.persona ?? ""}`}
      />
      <CardBody className="space-y-5">
        {sc.hooks.length ? (
          <div>
            <h3 className="mb-1.5 text-[13px] font-semibold text-ink-2">훅 후보</h3>
            <ol className="space-y-1">
              {sc.hooks.map((h, i) => (
                <li key={i} className={`flex items-start gap-2 rounded-lg px-3 py-1.5 text-sm ${i === sc.chosen_hook ? "bg-accent-soft font-medium text-ink" : "text-ink-2"}`}>
                  <span className="w-5 shrink-0 tabular-nums text-muted">{i + 1}</span>
                  <span className="flex-1">{h}</span>
                  {i === sc.chosen_hook ? <Pill tone="info">채택</Pill> : null}
                </li>
              ))}
            </ol>
          </div>
        ) : null}

        {sc.beats.length ? (
          <ol className="relative space-y-3 border-l border-line pl-4">
            {sc.beats.map((b, i) => (
              <li key={i} className="relative">
                <span className="absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full border-2 border-[var(--surface)] bg-accent" aria-hidden />
                <p className="text-xs font-semibold tabular-nums text-muted">
                  {fmtSec(b.start_sec)}–{fmtSec(b.end_sec)} · {ROLE_LABEL[b.role] ?? b.role}
                </p>
                <p className="text-sm text-ink">{b.narration}</p>
                {b.on_screen_text ? <p className="text-xs text-ink-2">화면 자막: {b.on_screen_text}</p> : null}
                {b.visual ? <p className="text-xs text-muted">연출: {b.visual}</p> : null}
                {b.claim_ids.length ? (
                  <p className="mt-1 flex flex-wrap gap-1">
                    {b.claim_ids.map((cid) => {
                      const c = claimText.get(cid);
                      return (
                        <Pill key={cid} tone={c?.grounded ? "good" : "critical"} icon={c?.grounded ? "check" : "alert"} title={c?.text}>
                          주장 {cid}
                        </Pill>
                      );
                    })}
                  </p>
                ) : null}
              </li>
            ))}
          </ol>
        ) : null}

        {sc.body ? <p className="whitespace-pre-wrap rounded-lg bg-surface-2 px-3 py-2 text-sm leading-relaxed text-ink">{sc.body}</p> : null}

        <div>
          <h3 className="mb-1.5 text-[13px] font-semibold text-ink-2">캡션</h3>
          <CaptionPreview text={sc.caption && sc.disclosure && !sc.caption.startsWith(sc.disclosure) ? `${sc.disclosure}\n${sc.caption}` : sc.caption || sc.disclosure} />
          {sc.hashtags.length ? <p className="mt-2 text-xs text-accent-ink">{sc.hashtags.map((h) => (h.startsWith("#") ? h : `#${h}`)).join(" ")}</p> : null}
          {sc.cta ? <p className="mt-1 text-xs text-muted">CTA: {sc.cta}</p> : null}
        </div>
      </CardBody>
    </Card>
  );
}

function ClaimsCard({ d }: { d: ContentDossier }) {
  const grounded = d.claims.filter((c) => c.grounded).length;
  return (
    <Card aria-labelledby="ct-claims">
      <CardHeader
        id="ct-claims"
        title="주장 · 근거"
        description="모든 사실 주장은 상품 팩트 필드에 근거해야 합니다 (C-01)"
        actions={<Pill tone={grounded === d.claims.length ? "good" : "critical"}>{grounded}/{d.claims.length} 근거 있음</Pill>}
      />
      <CardBody>
        {d.claims.length ? (
          <ul className="space-y-2">
            {d.claims.map((c) => (
              <li key={c.id} className="flex items-start gap-2.5 rounded-lg border border-line px-3 py-2">
                <Icon name={c.grounded ? "check" : "alert"} size={16} className={`mt-0.5 ${c.grounded ? "text-good-ink" : "text-critical-ink"}`} label={c.grounded ? "근거 있음" : "근거 없음"} />
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-ink">
                    <span className="mr-1.5 font-mono text-xs text-muted">{c.id}</span>
                    {c.text}
                  </p>
                  <p className="mt-1 flex flex-wrap items-center gap-1 text-xs">
                    {c.kind ? <span className="text-muted">{c.kind}</span> : null}
                    {c.evidence.length ? (
                      c.evidence.map((e) => (
                        <code key={e} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-2">
                          {e}
                        </code>
                      ))
                    ) : (
                      <span className="text-critical-ink">근거 필드 없음</span>
                    )}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted">태깅된 주장이 없습니다.</p>
        )}
      </CardBody>
    </Card>
  );
}

function ChecksCard({ d }: { d: ContentDossier }) {
  return (
    <Card aria-labelledby="ct-checks">
      <CardHeader id="ct-checks" title="게이트 검사 이력" description="FIX는 자동 수정 후 재검사, HOLD는 사람 승인, BLOCK은 배포 불가" />
      <CardBody>
        {d.checks.length ? (
          <ol className="relative space-y-4 border-l border-line pl-4">
            {d.checks.map((c) => {
              const fixed = c.findings.filter((f) => f.fixed).length;
              const open = c.findings.length - fixed;
              return (
                <li key={c.id} className="relative">
                  <span className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--surface)] bg-[var(--axis)]" aria-hidden />
                  <div className="flex flex-wrap items-center gap-2">
                    <VerdictPill verdict={c.verdict} />
                    <span className="text-sm font-medium text-ink">{STAGE_LABEL[c.stage ?? ""] ?? c.stage ?? "검사"}</span>
                    {c.iteration ? <span className="text-xs text-muted">#{c.iteration}</span> : null}
                    <span className="text-xs tabular-nums text-muted">{kst(c.decided_at)}</span>
                    {c.rule_set_version ? <span className="font-mono text-[11px] text-muted">rule_set {c.rule_set_version}</span> : null}
                  </div>
                  {c.findings.length ? (
                    <div className="mt-2">
                      <p className="mb-1 text-xs text-muted">
                        수정됨 {fixed} · 미해결 {open}
                      </p>
                      <FindingsList findings={c.findings} showDetails />
                    </div>
                  ) : (
                    <p className="mt-1 text-xs text-muted">지적 사항 없음</p>
                  )}
                </li>
              );
            })}
          </ol>
        ) : (
          <EmptyState icon="shield" title="아직 게이트 검사 전입니다" />
        )}
      </CardBody>
    </Card>
  );
}

export function ContentDetail({ id }: { id: string }) {
  const q = useContent(id);

  if (q.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/2" />
        <div className="grid gap-5 lg:grid-cols-[300px_1fr]">
          <Skeleton className="aspect-[9/16] max-h-[520px]" />
          <LoadingBlock rows={10} />
        </div>
      </div>
    );
  }
  if (q.error) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />;
  const d = q.data;
  const r = d.render;
  const lastVerdict = d.checks.length ? d.checks[d.checks.length - 1].verdict : null;

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-3 text-[13px] text-muted">
          {d.run_id ? (
            <Link href={`/runs/${encodeURIComponent(d.run_id)}`} className="inline-flex items-center gap-1 hover:text-ink">
              <Icon name="chevronLeft" size={14} /> 실행 {shortId(d.run_id)}
            </Link>
          ) : null}
          {d.parent_content_id ? (
            <Link href={`/contents/${encodeURIComponent(d.parent_content_id)}`} className="hover:text-ink">
              원본 콘텐츠 {shortId(d.parent_content_id)} (증폭 변형)
            </Link>
          ) : null}
        </div>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs text-muted">
              {KIND_LABEL[d.kind] ?? d.kind} · <span className="font-mono">{d.id}</span>
            </p>
            <h1 className="text-lg font-bold tracking-tight text-ink sm:text-xl">{d.product?.name ?? "콘텐츠 상세"}</h1>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <VerdictPill verdict={lastVerdict} />
            <Pill tone="neutral">상태 {d.status}</Pill>
          </div>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="생산 비용" value={krwPrecise(d.cost_krw, 1)} sub="LLM · TTS · 렌더 합계 (C_unit)" icon="calculator" />
        <StatTile label="최대 유사도" value={d.similarity_max !== null && d.similarity_max !== undefined ? pct(d.similarity_max, 0) : "—"} sub="레퍼런스·최근 30일 대비 (상한 80%)" icon="copy" />
        <StatTile label="게시" value={`${d.publications.filter((p) => p.state === "PUBLISHED").length} / ${d.publications.length}`} sub="PUBLISHED / 전체" icon="send" />
        <StatTile label="생성 (KST)" value={<span className="text-base">{kst(d.created_at)}</span>} sub={`${d.persona ?? ""} · ${d.template_id ?? ""}`} icon="clock" />
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <div className="space-y-5">
          <Card aria-labelledby="ct-media">
            <CardHeader id="ct-media" title={d.kind === "short" ? "렌더 결과" : "상품"} />
            <CardBody className="space-y-3">
              {d.kind === "short" ? (
                <div className="mx-auto max-w-[260px]">
                  <MediaPreview kind="short" mediaUrl={r?.media_url} thumbnailUrl={r?.thumbnail_url} alt={`${d.product?.name ?? ""} 영상`} />
                </div>
              ) : null}
              {d.product ? (
                <div className="flex items-center gap-3">
                  <Thumb src={d.product.image_url} alt={d.product.name} className="w-14 shrink-0 rounded-lg" />
                  <div className="min-w-0 text-sm">
                    <p className="font-medium text-ink">{d.product.name}</p>
                    <p className="text-xs text-muted">
                      {krw(d.product.price)}
                      {d.product.category_name ? ` · ${d.product.category_name}` : ""}
                      {d.product.is_rocket ? " · 로켓배송" : ""}
                    </p>
                    {d.product.product_url ? (
                      <a href={d.product.product_url} target="_blank" rel="noopener noreferrer" className="text-xs text-accent-ink hover:underline">
                        상품 페이지 <span className="sr-only">(새 창)</span>
                      </a>
                    ) : null}
                  </div>
                </div>
              ) : null}
              {r ? (
                <DefList
                  className="text-[13px]"
                  items={[
                    ["길이", r.duration_sec ? fmtSec(r.duration_sec) : "—"],
                    ["해상도", r.width && r.height ? `${r.width}×${r.height}${r.fps ? ` · ${r.fps}fps` : ""}` : "—"],
                    ["음량", r.loudness_lufs !== null && r.loudness_lufs !== undefined ? `${r.loudness_lufs} LUFS` : "—"],
                    ["보이스", r.voice_id ? `${r.voice_id}${r.voice_source ? ` (${r.voice_source})` : ""}` : "—"],
                    ["렌더러", r.renderer || "—"],
                    ["광고 오버레이", r.disclosure_overlay && Object.keys(r.disclosure_overlay).length ? "있음 (D-03)" : "—"],
                  ]}
                />
              ) : null}
            </CardBody>
          </Card>

          {d.review ? (
            <Card aria-labelledby="ct-review">
              <CardHeader id="ct-review" title="사람 검토" actions={<Pill tone={d.review.status === "pending" ? "warn" : d.review.status === "approved" ? "good" : "neutral"}>{d.review.status}</Pill>} />
              <CardBody className="space-y-2 text-sm">
                <ul className="space-y-1">
                  {d.review.reasons.map((x) => (
                    <li key={x} className="flex items-start gap-2">
                      <Icon name="pause" size={14} className="mt-0.5 text-serious-ink" />
                      {x}
                    </li>
                  ))}
                </ul>
                {d.review.decided_by ? <p className="text-xs text-muted">결정: {d.review.decided_by}</p> : null}
                {d.review.note ? <p className="text-xs text-ink-2">메모: {d.review.note}</p> : null}
                {d.review.status === "pending" ? (
                  <Link href="/holds" className="inline-flex items-center gap-1 text-[13px] text-accent-ink hover:underline">
                    HOLD 큐에서 검토 <Icon name="chevronRight" size={13} />
                  </Link>
                ) : null}
              </CardBody>
            </Card>
          ) : null}

          <Card aria-labelledby="ct-pubs">
            <CardHeader id="ct-pubs" title={`게시 (${d.publications.length})`} />
            <CardBody>
              {d.publications.length ? (
                <ul className="space-y-2">
                  {d.publications.map((p) => (
                    <li key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
                      <PlatformTag platform={p.platform} className="text-[13px]" />
                      <PublicationStatePill state={p.state} />
                      <span className="text-xs tabular-nums text-muted">{kst(p.scheduled_at)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted">게시 예정 없음</p>
              )}
              <Link href="/publications" className="mt-3 inline-flex items-center gap-1 text-[13px] text-accent-ink hover:underline">
                게시 현황에서 보기 <Icon name="chevronRight" size={13} />
              </Link>
            </CardBody>
          </Card>
        </div>

        <div className="min-w-0 space-y-5">
          <ScriptCard d={d} />
          <ChecksCard d={d} />
          <ClaimsCard d={d} />
        </div>
      </div>
    </div>
  );
}
