"use client";

import Link from "next/link";
import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock } from "@/components/ui/Feedback";
import { Segmented } from "@/components/ui/misc";
import { RunStatusPill } from "@/components/ui/Pill";
import { elapsed, kst, krw } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { runHasDuration, summaryText } from "@/lib/labels";
import { useRuns } from "@/lib/queries";
import type { RunStatus, RunSummary } from "@/lib/schemas";

type Filter = "all" | "active" | "approval" | "done" | "problem" | "rejected";

const FILTERS: Record<Filter, { label: string; match: (s: RunStatus) => boolean }> = {
  all: { label: "전체", match: () => true },
  active: { label: "진행 중", match: (s) => s === "running" || s === "approved" || s === "planned" },
  approval: { label: "승인 대기", match: (s) => s === "awaiting_approval" },
  done: { label: "완료", match: (s) => s === "completed" },
  problem: { label: "문제", match: (s) => s === "failed" || s === "paused_budget" },
  rejected: { label: "반려", match: (s) => s === "rejected" },
};

function Cost({ r }: { r: RunSummary }) {
  if (r.cost_krw !== null && r.cost_krw !== undefined) {
    return (
      <span>
        {krw(r.cost_krw)}
        <span className="text-muted"> / 추정 {krw(r.est_cost_krw)}</span>
      </span>
    );
  }
  return <span className="text-muted">추정 {krw(r.est_cost_krw)}</span>;
}

export function RunsList() {
  const runs = useRuns(50);
  const now = useNow();
  const [filter, setFilter] = useState<Filter>("all");
  const all = runs.data?.items ?? [];
  const items = all.filter((r) => FILTERS[filter].match(r.status));
  const count = (f: Filter) => all.filter((r) => FILTERS[f].match(r.status)).length;

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto pb-1">
        <Segmented<Filter>
          label="상태 필터"
          value={filter}
          onChange={setFilter}
          options={(Object.keys(FILTERS) as Filter[]).map((f) => ({
            value: f,
            label: (
              <span>
                {FILTERS[f].label}
                {runs.data ? <span className="ml-1 tabular-nums text-muted">{count(f)}</span> : null}
              </span>
            ),
          }))}
        />
      </div>

      <Card>
        {runs.isPending ? (
          <div className="p-5">
            <LoadingBlock rows={8} />
          </div>
        ) : runs.error ? (
          <div className="p-4">
            <ErrorBanner error={runs.error} onRetry={() => void runs.refetch()} retrying={runs.isFetching} />
          </div>
        ) : !items.length ? (
          <EmptyState icon="play" title={all.length ? "조건에 맞는 실행이 없습니다" : "아직 실행이 없습니다"}>
            {all.length ? "다른 필터를 선택하세요." : "명령 화면에서 첫 명령을 실행하세요."}
          </EmptyState>
        ) : (
          <>
            {/* Desktop table */}
            <table className="hidden w-full text-left text-sm md:table">
              <caption className="sr-only">명령 실행 목록</caption>
              <thead className="border-b border-line text-[13px] text-muted">
                <tr>
                  <th scope="col" className="px-5 py-3 font-medium">상태</th>
                  <th scope="col" className="px-3 py-3 font-medium">명령</th>
                  <th scope="col" className="px-3 py-3 text-right font-medium">비용</th>
                  <th scope="col" className="px-3 py-3 font-medium">시작 (KST)</th>
                  <th scope="col" className="px-5 py-3 text-right font-medium">소요</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.id} className="group border-b border-line last:border-0 hover:bg-surface-2">
                    <td className="whitespace-nowrap px-5 py-3 align-top">
                      <RunStatusPill status={r.status} />
                    </td>
                    <td className="max-w-xl px-3 py-3 align-top">
                      <Link href={`/runs/${encodeURIComponent(r.id)}`} className="font-medium text-ink group-hover:text-accent-ink hover:underline">
                        {r.raw_command}
                      </Link>
                      {summaryText(r.summary) ? <p className="mt-0.5 text-xs text-muted">{summaryText(r.summary)}</p> : null}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-right align-top tabular-nums">
                      <Cost r={r} />
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 align-top tabular-nums text-ink-2">{kst(r.started_at)}</td>
                    <td className="whitespace-nowrap px-5 py-3 text-right align-top tabular-nums text-ink-2">
                      {r.started_at && runHasDuration(r.status) ? elapsed(r.started_at, r.ended_at, now) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* Mobile cards */}
            <ul className="divide-y divide-line md:hidden">
              {items.map((r) => (
                <li key={r.id}>
                  <Link href={`/runs/${encodeURIComponent(r.id)}`} className="block space-y-1.5 px-4 py-3.5 active:bg-surface-2">
                    <div className="flex items-center justify-between gap-2">
                      <RunStatusPill status={r.status} />
                      <span className="text-xs tabular-nums text-muted">{kst(r.started_at)}</span>
                    </div>
                    <p className="text-sm font-medium text-ink">{r.raw_command}</p>
                    {summaryText(r.summary) ? <p className="text-xs text-muted">{summaryText(r.summary)}</p> : null}
                    <p className="text-xs tabular-nums">
                      <Cost r={r} />
                    </p>
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </div>
  );
}
