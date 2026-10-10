"use client";

import Link from "next/link";
import { Icon } from "@/components/ui/Icon";
import { Thumb } from "@/components/ui/misc";
import { Pill, PlatformTag, PublicationStatePill, VerdictPill } from "@/components/ui/Pill";
import { kst, krw } from "@/lib/format";
import { KIND_LABEL, VERDICT } from "@/lib/labels";
import type { Content, Finding, Publication } from "@/lib/schemas";

export function FindingsList({ findings, showDetails = false }: { findings: Finding[]; showDetails?: boolean }) {
  if (!findings.length) return <p className="text-[13px] text-muted">지적 사항 없음</p>;
  return (
    <ul className="space-y-1.5">
      {findings.map((f, i) => {
        const v = VERDICT[f.verdict];
        return (
          <li key={`${f.rule_id}-${i}`} className="flex items-start gap-2 text-[13px]">
            <span className="mt-px shrink-0">
              <Pill tone={f.fixed ? "good" : v.tone} icon={f.fixed ? "check" : f.verdict === "BLOCK" ? "ban" : f.verdict === "HOLD" ? "pause" : "sparkle"}>
                <span className="font-mono">{f.rule_id}</span>
              </Pill>
            </span>
            <span className="min-w-0 flex-1 text-ink">
              {f.message}
              <span className="ml-1.5 text-xs text-muted">
                {f.verdict}
                {f.fixed ? " · 자동 수정됨" : ""}
              </span>
              {showDetails &&
              f.details !== undefined &&
              f.details !== null &&
              f.details !== "" &&
              !(typeof f.details === "object" && Object.keys(f.details as object).length === 0) ? (
                <code className="mt-1 block overflow-x-auto whitespace-pre rounded bg-surface-2 px-2 py-1 font-mono text-[11px] text-ink-2">
                  {typeof f.details === "string" ? f.details : JSON.stringify(f.details)}
                </code>
              ) : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function PublicationsTable({ publications }: { publications: Publication[] }) {
  if (!publications.length) return <p className="text-[13px] text-muted">게시 예정 없음</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[460px] text-left text-[13px]">
        <caption className="sr-only">게시 현황</caption>
        <thead className="text-muted">
          <tr className="border-b border-line">
            <th scope="col" className="py-1.5 pr-3 font-medium">플랫폼</th>
            <th scope="col" className="py-1.5 pr-3 font-medium">상태</th>
            <th scope="col" className="py-1.5 pr-3 font-medium">예약 / 게시 (KST)</th>
            <th scope="col" className="py-1.5 font-medium">링크 · subId</th>
          </tr>
        </thead>
        <tbody>
          {publications.map((p) => (
            <tr key={p.id} className="border-b border-line last:border-0">
              <td className="py-2 pr-3 align-top">
                <PlatformTag platform={p.platform} className="text-[13px]" />
                <div className="text-xs text-muted">{p.account_handle}</div>
              </td>
              <td className="py-2 pr-3 align-top">
                <PublicationStatePill state={p.state} />
              </td>
              <td className="py-2 pr-3 align-top tabular-nums text-ink-2">
                <div>{kst(p.scheduled_at)}</div>
                {p.published_at ? <div className="text-xs text-good-ink">게시 {kst(p.published_at)}</div> : null}
              </td>
              <td className="py-2 align-top">
                {p.permalink ? (
                  <a href={p.permalink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent-ink hover:underline">
                    게시물 열기 <Icon name="external" size={12} />
                    <span className="sr-only">(새 창)</span>
                  </a>
                ) : (
                  <span className="text-muted">—</span>
                )}
                {p.sub_id ? <div className="font-mono text-[11px] text-muted">{p.sub_id}</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MediaPreview({
  kind,
  mediaUrl,
  thumbnailUrl,
  alt,
  className = "",
}: {
  kind: string;
  mediaUrl?: string | null;
  thumbnailUrl?: string | null;
  alt: string;
  className?: string;
}) {
  if (mediaUrl && kind === "short") {
    return (
      <video
        className={`aspect-[9/16] w-full rounded-lg bg-black object-contain ${className}`}
        src={mediaUrl}
        poster={thumbnailUrl ?? undefined}
        controls
        playsInline
        preload="metadata"
        aria-label={alt}
      />
    );
  }
  return (
    <Thumb
      src={thumbnailUrl}
      alt={alt}
      ratio={kind === "short" ? "portrait" : "square"}
      fallbackIcon={kind === "short" ? "film" : "text"}
      className={`rounded-lg ${className}`}
    />
  );
}

export function ContentCard({ content, index }: { content: Content; index: number }) {
  const c = content;
  const name = c.product?.name ?? "상품 정보 없음";
  return (
    <article className="flex flex-col gap-4 rounded-xl border border-line bg-surface p-4 shadow-card sm:flex-row" aria-labelledby={`content-${c.id}`}>
      <div className="w-full shrink-0 sm:w-32 lg:w-36">
        <div className="mx-auto max-w-40 sm:max-w-none">
          <MediaPreview kind={c.kind} mediaUrl={c.media_url} thumbnailUrl={c.thumbnail_url ?? (c.kind === "text" ? c.product?.image_url : null)} alt={`${name} 미리보기`} />
        </div>
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-xs text-muted">
              #{index + 1} · {KIND_LABEL[c.kind] ?? c.kind} · <span className="font-mono">{c.id}</span>
            </p>
            <h3 id={`content-${c.id}`} className="font-semibold text-ink">
              <Link href={`/contents/${encodeURIComponent(c.id)}`} className="hover:text-accent-ink hover:underline">
                {name}
              </Link>
              {c.product?.price ? <span className="ml-2 text-sm font-normal text-muted">{krw(c.product.price)}</span> : null}
            </h3>
          </div>
          <VerdictPill verdict={c.verdict} />
        </div>
        {c.hook ? <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-ink">“{c.hook}”</p> : null}
        <p className="text-xs text-muted">
          {c.persona ? `페르소나 ${c.persona}` : null}
          {c.template_id ? ` · 템플릿 ${c.template_id}` : null} · 상태 {c.status}
        </p>
        <div>
          <h4 className="mb-1.5 text-xs font-semibold text-ink-2">M7 게이트 지적 사항</h4>
          <FindingsList findings={c.findings} />
          {c.verdict === "HOLD" ? (
            <Link href="/holds" className="mt-2 inline-flex items-center gap-1 text-[13px] text-accent-ink hover:underline">
              HOLD 큐에서 검토 <Icon name="chevronRight" size={13} />
            </Link>
          ) : null}
        </div>
        <div>
          <h4 className="mb-1.5 text-xs font-semibold text-ink-2">게시</h4>
          <PublicationsTable publications={c.publications} />
        </div>
      </div>
    </article>
  );
}
