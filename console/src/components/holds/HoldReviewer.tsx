"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";
import { FindingsList, MediaPreview } from "@/components/runs/ContentCard";
import { CaptionPreview } from "@/components/ui/CaptionPreview";
import { Button, IconButton } from "@/components/ui/Button";
import { EmptyState, ErrorBanner, Kbd, LoadingBlock, Notice, Skeleton } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Thumb } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { HOLD_SECONDS_PER_ITEM } from "@/lib/config";
import { kst, krw, relative } from "@/lib/format";
import { useHotkeys, useNow } from "@/lib/hooks";
import { KIND_LABEL } from "@/lib/labels";
import { qk, useHoldDecision, useHolds, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { HoldItem, HoldList } from "@/lib/schemas";

function ScriptText({ text }: { text: string | null | undefined }) {
  if (!text) return <p className="text-[13px] text-muted">스크립트 없음</p>;
  return (
    <ol className="space-y-1 text-sm leading-relaxed">
      {text.split("\n").map((line, i) => {
        const m = /^\[([^\]]+)\]\s*(.*)$/.exec(line);
        return (
          <li key={i} className="flex gap-2">
            {m ? (
              <>
                <span className="w-16 shrink-0 font-mono text-xs leading-6 text-muted">{m[1]}</span>
                <span className="text-ink">{m[2]}</span>
              </>
            ) : (
              <span className="text-ink">{line || " "}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** Seconds spent on the current item (resets per item via `key`). */
function ItemTimer() {
  const [s, setS] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setS((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const over = s > HOLD_SECONDS_PER_ITEM;
  return (
    <span className={`inline-flex items-center gap-1 text-xs tabular-nums ${over ? "text-serious-ink" : "text-muted"}`} aria-label={`이 항목 검토 ${s}초 경과`}>
      <Icon name="clock" size={13} />
      {s}초 / {HOLD_SECONDS_PER_ITEM}초
    </span>
  );
}

function HoldCard({ item }: { item: HoldItem }) {
  const now = useNow();
  const productName = item.product?.name ?? "상품 정보 없음";
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
      <div className="space-y-3">
        {item.kind === "short" || item.media_url || item.thumbnail_url ? (
          <div className="mx-auto w-full max-w-[200px] sm:max-w-[260px] lg:max-w-none">
            <MediaPreview kind={item.kind} mediaUrl={item.media_url} thumbnailUrl={item.thumbnail_url} alt={`${productName} 렌더 미리보기`} />
          </div>
        ) : null}
        <div className="flex items-center gap-3 rounded-xl border border-line bg-surface p-3">
          <Thumb src={item.product?.image_url} alt={productName} className="w-12 shrink-0 rounded-lg" />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink">{productName}</p>
            <p className="text-xs text-muted">
              {krw(item.product?.price)} · {KIND_LABEL[item.kind] ?? item.kind}
            </p>
          </div>
        </div>
      </div>

      <div className="min-w-0 space-y-5">
        <section aria-labelledby="hold-reasons">
          <h2 id="hold-reasons" className="mb-2 text-[13px] font-semibold text-ink-2">
            HOLD 사유
          </h2>
          <ul className="flex flex-wrap gap-2">
            {item.reasons.map((r) => (
              <li key={r}>
                <Pill tone="serious" icon="pause" className="h-auto whitespace-normal py-1">
                  {r}
                </Pill>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted">
            게이트 판정 {relative(item.created_at, now)} · {kst(item.created_at)} KST · 콘텐츠{" "}
            <Link href={`/contents/${encodeURIComponent(item.content_id)}`} className="font-mono text-accent-ink hover:underline">
              {item.content_id}
            </Link>
            {item.run_id ? (
              <>
                {" · "}
                <Link href={`/runs/${encodeURIComponent(item.run_id)}`} className="text-accent-ink hover:underline">
                  실행 보기
                </Link>
              </>
            ) : null}
          </p>
        </section>

        {item.hook ? (
          <section aria-labelledby="hold-hook">
            <h2 id="hold-hook" className="mb-1.5 text-[13px] font-semibold text-ink-2">
              훅 (첫 3초)
            </h2>
            <p className="rounded-lg bg-surface-2 px-3 py-2 text-base font-medium text-ink">“{item.hook}”</p>
          </section>
        ) : null}

        <section aria-labelledby="hold-caption">
          <h2 id="hold-caption" className="mb-1.5 text-[13px] font-semibold text-ink-2">
            캡션 미리보기
          </h2>
          <CaptionPreview text={item.caption_preview} />
        </section>

        <section aria-labelledby="hold-script">
          <h2 id="hold-script" className="mb-1.5 text-[13px] font-semibold text-ink-2">
            {item.kind === "short" ? "대본" : "본문"}
          </h2>
          <ScriptText text={item.script_text} />
        </section>

        <section aria-labelledby="hold-findings">
          <h2 id="hold-findings" className="mb-1.5 text-[13px] font-semibold text-ink-2">
            M7 지적 사항
          </h2>
          <FindingsList findings={item.findings} showDetails />
        </section>
      </div>
    </div>
  );
}

export function HoldReviewer() {
  const holds = useHolds();
  const qc = useQueryClient();
  const toast = useToast();
  const decide = useHoldDecision();
  const { can, role } = usePrincipal();
  const canReview = can("reviewHold");

  const [index, setIndex] = useState(0);
  const [note, setNote] = useState("");
  const [decided, setDecided] = useState<Set<string>>(() => new Set());
  const [showKeys, setShowKeys] = useState(false);

  const queue = (holds.data?.items ?? []).filter((h) => !decided.has(h.id));
  const pos = Math.min(index, Math.max(0, queue.length - 1));
  const item = queue[pos];
  const minutes = Math.ceil((queue.length * HOLD_SECONDS_PER_ITEM) / 60);

  const go = (delta: number) => setIndex(Math.max(0, Math.min(queue.length - 1, pos + delta)));

  const submit = (approve: boolean) => {
    if (!item || !canReview) return;
    const id = item.id;
    const n = note.trim();
    setDecided((s) => new Set(s).add(id));
    setNote("");
    decide.mutate(
      { id, approve, note: n || undefined },
      {
        onSuccess: () => {
          qc.setQueryData<HoldList>(qk.holds, (old) => (old ? { items: old.items.filter((h) => h.id !== id) } : old));
          setDecided((s) => {
            const next = new Set(s);
            next.delete(id);
            return next;
          });
          toast(approve ? "승인 — 배포 대기열로 이동했습니다" : "반려했습니다", approve ? "good" : "info");
        },
        onError: (e) => {
          setDecided((s) => {
            const next = new Set(s);
            next.delete(id);
            return next;
          });
          toast(`처리 실패: ${e instanceof Error ? e.message : "알 수 없는 오류"}`, "critical");
        },
      },
    );
  };

  useHotkeys(
    {
      a: () => submit(true),
      r: () => submit(false),
      j: () => go(1),
      k: () => go(-1),
      arrowright: () => go(1),
      arrowleft: () => go(-1),
      "?": () => setShowKeys((v) => !v),
    },
    !!holds.data,
  );

  if (holds.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-6 w-64" />
        <div className="grid gap-5 lg:grid-cols-[300px_1fr]">
          <Skeleton className="aspect-[9/16] max-h-[520px]" />
          <LoadingBlock rows={10} />
        </div>
      </div>
    );
  }
  if (holds.error) return <ErrorBanner error={holds.error} onRetry={() => void holds.refetch()} retrying={holds.isFetching} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface px-4 py-3 shadow-card">
        <p className="text-sm text-ink" aria-live="polite">
          오늘 남은 검토 <strong className="text-lg tabular-nums">{queue.length}</strong>건 · 예상 <strong className="tabular-nums">{minutes}</strong>분
          <span className="ml-2 text-xs text-muted">(건당 {HOLD_SECONDS_PER_ITEM}초 기준)</span>
        </p>
        {item ? (
          <div className="flex items-center gap-2">
            <ItemTimer key={item.id} />
            <span className="text-sm tabular-nums text-ink-2" aria-label={`${queue.length}건 중 ${pos + 1}번째`}>
              {pos + 1} / {queue.length}
            </span>
            <IconButton icon="chevronLeft" label="이전 (K)" onClick={() => go(-1)} disabled={pos === 0} />
            <IconButton icon="chevronRight" label="다음 (J)" onClick={() => go(1)} disabled={pos >= queue.length - 1} />
            <IconButton icon="keyboard" label="단축키 보기 (?)" onClick={() => setShowKeys((v) => !v)} aria-expanded={showKeys} className="hidden sm:inline-flex" />
          </div>
        ) : null}
      </div>

      {showKeys ? (
        <div className="flex flex-wrap gap-x-5 gap-y-2 rounded-lg bg-surface-2 px-4 py-2.5 text-[13px] text-ink-2">
          <span><Kbd>A</Kbd> 승인</span>
          <span><Kbd>R</Kbd> 반려 (사유 입력란 내용 첨부)</span>
          <span><Kbd>J</Kbd>/<Kbd>→</Kbd> 다음</span>
          <span><Kbd>K</Kbd>/<Kbd>←</Kbd> 이전</span>
          <span><Kbd>?</Kbd> 도움말</span>
        </div>
      ) : null}

      {!canReview ? (
        <Notice tone="warn" title="열람 전용">
          HOLD 승인·반려는 {ALLOWED_ROLES.reviewHold}만 할 수 있습니다{role ? ` (현재 역할: ${role})` : ""}.
        </Notice>
      ) : null}

      {!item ? (
        <EmptyState icon="check" title="HOLD 큐가 비었습니다">
          검토할 항목이 없습니다. 게이트가 HOLD를 내리면 여기에 쌓입니다 (신규 계정 14일, 신규 템플릿 10건, 상표·보이스 의심 등).
        </EmptyState>
      ) : (
        <>
          <article key={item.id} aria-label={`HOLD 항목 ${pos + 1}: ${item.product?.name ?? item.content_id}`} className="rounded-2xl border border-line bg-surface p-4 shadow-card sm:p-5">
            <HoldCard item={item} />
          </article>

          {canReview ? (
            <div className="fixed inset-x-0 bottom-0 z-20 border-t border-line bg-surface/95 px-4 pb-[max(env(safe-area-inset-bottom),12px)] pt-3 backdrop-blur lg:sticky lg:bottom-4 lg:rounded-2xl lg:border lg:px-5 lg:pb-3 lg:shadow-xl">
              <div className="mx-auto flex max-w-[1400px] flex-col gap-2.5 lg:flex-row lg:items-center">
                <label htmlFor="hold-note" className="sr-only">
                  반려 사유 (선택)
                </label>
                <input
                  id="hold-note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") (e.target as HTMLInputElement).blur();
                  }}
                  placeholder="메모 / 반려 사유 (선택) — Esc로 단축키 모드"
                  className="h-11 min-w-0 flex-1 rounded-lg border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none"
                />
                <div className="grid grid-cols-2 gap-2 lg:flex">
                  <Button variant="secondary" size="lg" icon="x" onClick={() => submit(false)} className="lg:min-w-32">
                    반려 <span className="hidden lg:inline-flex"><Kbd>R</Kbd></span>
                  </Button>
                  <Button variant="success" size="lg" icon="check" onClick={() => submit(true)} className="lg:min-w-32">
                    승인 <span className="hidden lg:inline-flex"><Kbd>A</Kbd></span>
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
