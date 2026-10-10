"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { ErrorBanner, Notice } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Pill, PlatformTag, PublicationStatePill, VerdictPill } from "@/components/ui/Pill";
import { kst } from "@/lib/format";
import { PLATFORM_LABEL } from "@/lib/labels";
import type { Platform, StudioPublishResult } from "@/lib/schemas";
import { hasDisclosure } from "@/components/ui/CaptionPreview";
import { FindingsList } from "@/components/runs/ContentCard";
import { captionCss } from "./shared";

export interface PreviewModel {
  kind: "short" | "text";
  backgroundUrl: string | null;
  backgroundIsVideo: boolean;
  disclosure: string;
  disclosureSeconds: number;
  caption: string;
  captionStyle: string | null | undefined;
  captionPosition: string | null | undefined;
  captionsEnabled: boolean;
  watermark: string;
  body: string;
  handle: string;
  mediaUrl: string | null;
  posterUrl: string | null;
  rendered: boolean;
}

const POS: Record<string, string> = { upper: "top-[22%]", middle: "top-1/2 -translate-y-1/2", bottom: "bottom-[16%]" };

/** 9:16 phone frame: live CSS mock before render, the real <video> after, Threads-style card for text. */
export function PhonePreview({ m }: { m: PreviewModel }) {
  const [failedBg, setFailedBg] = useState<string | null>(null);
  if (m.kind === "text") {
    const [first, ...rest] = m.body.split("\n");
    const ok = hasDisclosure(first ?? "");
    return (
      <div className="mx-auto w-full max-w-[320px] rounded-[28px] border-[6px] border-[#111] bg-surface p-4 shadow-xl dark:border-[#333]">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-surface-3 text-sm font-bold text-ink-2" aria-hidden>
            {m.handle.replace(/^@/, "").slice(0, 1).toUpperCase() || "A"}
          </span>
          <div>
            <p className="text-sm font-semibold text-ink">{m.handle || "@account"}</p>
            <p className="text-[11px] text-muted">방금 · 스레드 미리보기</p>
          </div>
        </div>
        <p className={`mt-3 rounded-md px-2 py-1 text-[13px] font-medium ${ok ? "bg-good-soft text-ink" : "bg-critical-soft text-ink"}`}>{first || "(첫 줄 비어 있음)"}</p>
        <p className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap text-[13px] leading-relaxed text-ink">{rest.join("\n").trim()}</p>
        <p className={`mt-3 flex items-center gap-1 text-[11px] ${ok ? "text-good-ink" : "text-critical-ink"}`}>
          <Icon name={ok ? "check" : "alert"} size={12} /> {ok ? "첫 줄 대가성 문구 확인" : "첫 줄에 대가성 문구가 필요합니다"}
        </p>
      </div>
    );
  }

  const cap = captionCss(m.captionStyle);
  return (
    <div className="mx-auto w-full max-w-[300px]">
      <div className="relative aspect-[9/16] overflow-hidden rounded-[30px] border-[6px] border-[#111] bg-black shadow-xl dark:border-[#333]">
        {m.rendered && m.mediaUrl ? (
          <video src={m.mediaUrl} poster={m.posterUrl ?? undefined} controls playsInline preload="metadata" className="h-full w-full object-contain" aria-label="렌더된 쇼츠" />
        ) : m.rendered && m.posterUrl ? (
          <>
            <Image src={m.posterUrl} alt="렌더 결과 포스터" fill unoptimized sizes="300px" className="object-cover" />
            <span className="absolute inset-x-0 bottom-3 mx-auto w-fit rounded-full bg-black/60 px-2.5 py-1 text-[11px] text-white">렌더 완료 · 포스터</span>
          </>
        ) : (
          <>
            {m.backgroundUrl && failedBg !== m.backgroundUrl ? (
              m.backgroundIsVideo ? (
                <video
                  src={m.backgroundUrl}
                  muted
                  playsInline
                  autoPlay
                  loop
                  className="absolute inset-0 h-full w-full object-cover"
                  aria-hidden
                  onError={() => setFailedBg(m.backgroundUrl)}
                />
              ) : (
                <Image
                  src={m.backgroundUrl}
                  alt=""
                  fill
                  unoptimized
                  sizes="300px"
                  className="object-cover"
                  aria-hidden
                  onError={() => setFailedBg(m.backgroundUrl)}
                />
              )
            ) : (
              <div className="absolute inset-0 bg-gradient-to-b from-[#2b3442] to-[#5a4636]" aria-hidden />
            )}
            <div className="absolute inset-0 bg-gradient-to-b from-black/10 via-transparent to-black/40" aria-hidden />
            {/* D-03: locked disclosure band, first seconds */}
            <div className="absolute inset-x-0 top-0 bg-black/60 px-2 py-1.5 text-center text-[10px] font-medium leading-tight text-white">
              {m.disclosure}
              <span className="ml-1 rounded bg-white/20 px-1 text-[9px]">0–{m.disclosureSeconds}초</span>
            </div>
            {m.captionsEnabled && m.caption ? (
              <div className={`absolute inset-x-3 text-center ${POS[m.captionPosition ?? "bottom"] ?? POS.bottom}`}>
                <span className={`inline text-[17px] leading-snug [box-decoration-break:clone] ${cap.className}`} style={cap.style}>
                  {m.caption}
                </span>
              </div>
            ) : null}
            {m.watermark ? <span className="absolute bottom-2 right-3 text-[10px] font-semibold text-white/75 [text-shadow:0_1px_3px_rgba(0,0,0,.6)]">{m.watermark}</span> : null}
            <span className="absolute left-2 top-9 rounded bg-black/45 px-1.5 py-0.5 text-[9px] text-white/80">미리보기</span>
          </>
        )}
      </div>
    </div>
  );
}

/** Elapsed seconds since `since` (ms), ticking each second while active. */
function useElapsed(since: number | null): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!since) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [since]);
  return since && now ? Math.max(0, Math.round((now - since) / 1000)) : 0;
}

export function RenderProgress({ since, ai = false }: { since: number | null; ai?: boolean }) {
  const s = useElapsed(since);
  return (
    <div role="status" aria-live="polite" className="space-y-1.5">
      <div className="h-1.5 overflow-hidden rounded-full bg-accent-soft">
        <div className="h-full w-1/3 animate-[studio-indeterminate_1.4s_ease-in-out_infinite] rounded-full bg-accent" />
      </div>
      <p className="text-xs tabular-nums text-muted">
        렌더 중… {s ? `${s}초 경과 · ` : ""}
        {ai ? "AI 장면 생성 포함 보통 1–3분 (AI 장면 → TTS → 자막 정렬 → 합성)" : "보통 10–30초 (TTS → 자막 정렬 → 합성)"}
      </p>
    </div>
  );
}

const VERDICT_TEXT: Record<string, string> = {
  PASS: "게이트 통과 — 계정별 시간대에 분산 예약했습니다.",
  FIX: "자동 수정으로 해결되지 않은 문제가 남았습니다.",
  HOLD: "사람 승인이 필요합니다 — HOLD 큐에서 승인하면 예약됩니다.",
  BLOCK: "배포할 수 없습니다 — 지적 사항(권리·규제 카테고리 등)을 확인하세요.",
};

export function PublishResult({
  result,
  onNewVariant,
  newVariantPending,
  contentId,
}: {
  result: StudioPublishResult;
  onNewVariant?: () => void;
  newVariantPending?: boolean;
  contentId?: string | null;
}) {
  const regenerate = result.verdict === "FIX" && result.actions.includes("regenerate");
  const tone = result.verdict === "PASS" ? "border-good/40 bg-good-soft" : result.verdict === "BLOCK" ? "border-critical/40 bg-critical-soft" : "border-warn/50 bg-warn-soft";
  return (
    <div className={`space-y-3 rounded-xl border p-3 ${tone}`} role="status" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <VerdictPill verdict={result.verdict} />
        <span className="text-sm font-medium text-ink">{VERDICT_TEXT[result.verdict]}</span>
      </div>
      {regenerate ? (
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-ink-2">
          대본을 고쳐 다시 게시하거나, 같은 상품으로 새 변형을 만드세요.
          {onNewVariant ? (
            <Button size="sm" icon="sparkle" loading={newVariantPending} onClick={onNewVariant}>
              새 변형 만들기
            </Button>
          ) : null}
        </div>
      ) : null}
      {result.verdict === "HOLD" ? (
        <Link href="/holds" className="inline-flex items-center gap-1 text-[13px] font-medium text-accent-ink hover:underline">
          HOLD 큐로 이동 <Icon name="chevronRight" size={13} />
        </Link>
      ) : null}
      {result.actions.length ? <p className="text-xs text-muted">게이트 조치: {result.actions.join(", ")}</p> : null}
      {result.findings.length ? <FindingsList findings={result.findings} /> : null}
      {result.publications.length ? (
        <ul className="space-y-1.5">
          {result.publications.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
              <PlatformTag platform={p.platform} className="text-[13px]" />
              <PublicationStatePill state={p.state} />
              <span className="text-xs tabular-nums text-muted">{kst(p.scheduled_at)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex flex-wrap gap-3 text-[13px]">
        {result.publications.length ? (
          <Link href="/publications" className="text-accent-ink hover:underline">
            게시 현황 보기
          </Link>
        ) : null}
        {contentId ? (
          <Link href={`/contents/${encodeURIComponent(contentId)}`} className="text-accent-ink hover:underline">
            콘텐츠 상세
          </Link>
        ) : null}
      </div>
    </div>
  );
}

export interface TargetAccount {
  id: string;
  platform: Platform;
  handle: string;
  status: string;
}

export function TargetChips({
  accounts,
  selected,
  onToggle,
  disabled,
  label = "게시 대상",
}: {
  accounts: TargetAccount[];
  selected: string[];
  onToggle: (id: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  if (!accounts.length) return <p className="text-xs text-muted">게시할 수 있는 활성 계정이 없습니다.</p>;
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
      {accounts.map((a) => {
        const on = selected.includes(a.id);
        return (
          <button
            key={a.id}
            type="button"
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onToggle(a.id)}
            title={`${PLATFORM_LABEL[a.platform]} ${a.handle}`}
            className={`inline-flex h-8 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium transition-colors disabled:opacity-50 ${
              on ? "border-accent bg-accent-soft text-accent-ink" : "border-line-strong bg-surface text-ink-2 hover:border-accent"
            }`}
          >
            {on ? <Icon name="check" size={12} /> : null}
            <PlatformTag platform={a.platform} className="text-xs" />
            <span className="max-w-24 truncate">{a.handle}</span>
          </button>
        );
      })}
    </div>
  );
}

export function RenderFailed({ error }: { error: string | null | undefined }) {
  return (
    <Notice tone="warn" title="렌더 실패">
      <span className="break-all">{error || "알 수 없는 오류"}</span> — 소스·보이스를 바꾸거나 다시 시도하세요.
    </Notice>
  );
}

export { ErrorBanner, Pill };
