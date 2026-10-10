"use client";

import { useDeferredValue, useState } from "react";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock } from "@/components/ui/Feedback";
import { Select, TextInput } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/misc";
import { kst } from "@/lib/format";
import { useAudit } from "@/lib/queries";

function payloadText(p: unknown): string {
  if (p === undefined || p === null) return "";
  if (typeof p === "string") return p;
  try {
    return JSON.stringify(p, null, 2);
  } catch {
    return String(p);
  }
}

function payloadSummary(p: unknown): string {
  if (p === undefined || p === null) return "—";
  if (typeof p !== "object") return String(p);
  const entries = Object.entries(p as Record<string, unknown>);
  if (!entries.length) return "{}";
  return entries
    .slice(0, 3)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

export function AuditView() {
  const q = useAudit(200);
  const [text, setText] = useState("");
  const [action, setAction] = useState("");
  const query = useDeferredValue(text.trim().toLowerCase());
  const all = q.data?.items ?? [];
  const actions = Array.from(new Set(all.map((e) => e.action))).sort();
  const items = all.filter(
    (e) =>
      (!action || e.action === action) &&
      (!query || `${e.actor} ${e.action} ${e.target ?? ""} ${payloadSummary(e.payload)}`.toLowerCase().includes(query)),
  );

  return (
    <>
      <PageHeader title="감사 로그" description="모든 게시 · 승인 · 규칙 변경 기록 (최근 200건, 1년 보관 — §10)." />
      <div className="mb-4 flex flex-col gap-2 sm:flex-row">
        <div className="flex-1">
          <label htmlFor="audit-q" className="sr-only">
            검색
          </label>
          <TextInput id="audit-q" type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="행위자, 대상, 내용 검색" />
        </div>
        <div className="sm:w-60">
          <label htmlFor="audit-action" className="sr-only">
            액션 필터
          </label>
          <Select id="audit-action" value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">모든 액션</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <Card>
        {q.isPending ? (
          <div className="p-5">
            <LoadingBlock rows={10} />
          </div>
        ) : q.error ? (
          <div className="p-4">
            <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
          </div>
        ) : !items.length ? (
          <EmptyState icon="scroll" title={all.length ? "조건에 맞는 기록이 없습니다" : "감사 기록이 없습니다"} />
        ) : (
          <>
            <p className="border-b border-line px-5 py-2 text-xs text-muted" aria-live="polite">
              {items.length}건 표시 / 전체 {all.length}건
            </p>
            <ul className="divide-y divide-line">
              {items.map((e, i) => (
                <li key={`${e.at}-${i}`} className="px-4 py-2.5 sm:px-5">
                  <details className="group">
                    <summary className="flex cursor-pointer list-none flex-col gap-1 sm:flex-row sm:items-center sm:gap-4 [&::-webkit-details-marker]:hidden">
                      <span className="w-36 shrink-0 text-xs tabular-nums text-muted">{kst(e.at)}</span>
                      <span className="w-48 shrink-0 truncate text-[13px] text-ink-2" title={e.actor}>
                        {e.actor}
                      </span>
                      <code className="w-44 shrink-0 truncate rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-ink">{e.action}</code>
                      <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-2" title={e.target ?? ""}>
                        {e.target ?? "—"}
                      </span>
                      <span className="hidden min-w-0 max-w-xs truncate text-xs text-muted lg:block">{payloadSummary(e.payload)}</span>
                    </summary>
                    <pre className="mt-2 overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-xs text-ink-2">{payloadText(e.payload) || "payload 없음"}</pre>
                  </details>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </>
  );
}
