"use client";

import Link from "next/link";
import { PlanPreview } from "@/components/command/PlanPreview";
import { Card, CardBody, CardHeader, DefList } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock, Skeleton } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { Meter, StatTile } from "@/components/ui/misc";
import { Pill, RunStatusPill } from "@/components/ui/Pill";
import { elapsed, kst, krw, pct } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { VERDICT, runHasDuration, summaryText } from "@/lib/labels";
import { useRun } from "@/lib/queries";
import { VERDICTS } from "@/lib/schemas";
import { ContentCard } from "./ContentCard";

export function RunDetail({ id }: { id: string }) {
  const q = useRun(id);
  const now = useNow();

  const back = (
    <Link href="/runs" className="inline-flex items-center gap-1 text-[13px] text-muted hover:text-ink">
      <Icon name="chevronLeft" size={14} /> 실행 목록
    </Link>
  );

  if (q.isPending) {
    return (
      <div className="space-y-5">
        {back}
        <Skeleton className="h-8 w-2/3" />
        <div className="grid gap-3 sm:grid-cols-3">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
        <LoadingBlock rows={6} />
      </div>
    );
  }
  if (q.error) {
    return (
      <div className="space-y-4">
        {back}
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </div>
    );
  }

  const { run, contents, cost } = q.data;
  const verdictCounts = Object.fromEntries(VERDICTS.map((v) => [v, contents.filter((c) => c.verdict === v).length]));
  const pending = contents.filter((c) => !c.verdict).length;
  const pubs = contents.flatMap((c) => c.publications);
  const published = pubs.filter((p) => p.state === "PUBLISHED").length;
  const capFrac = cost.cap_krw ? cost.spent_krw / cost.cap_krw : null;

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        {back}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <h1 className="max-w-4xl text-lg font-bold leading-snug tracking-tight text-ink sm:text-xl">{run.raw_command}</h1>
          <div className="flex items-center gap-2">
            {q.isFetching ? <span className="text-xs text-muted">갱신 중…</span> : null}
            <RunStatusPill status={run.status} />
          </div>
        </div>
        {summaryText(run.summary) ? <p className="text-sm text-muted">{summaryText(run.summary)}</p> : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-line bg-surface p-4 shadow-card sm:col-span-2">
          <div className="flex items-baseline justify-between gap-2 text-[13px] text-muted">
            <span>누적 비용 / 예산 상한</span>
            {capFrac !== null ? <span className="tabular-nums">{pct(capFrac, 0)}</span> : null}
          </div>
          <p className="mt-1.5 text-xl font-semibold tracking-tight sm:text-2xl">
            {krw(cost.spent_krw)} <span className="text-base font-normal text-muted">/ {krw(cost.cap_krw)}</span>
          </p>
          <Meter className="mt-2.5" value={cost.spent_krw} max={cost.cap_krw} label="누적 비용 대비 예산 상한" valueText={`${krw(cost.spent_krw)} / ${krw(cost.cap_krw)}`} />
          <p className="mt-1.5 text-xs text-muted">추정 {krw(run.est_cost_krw)} · 상한 도달 시 남은 상품은 일시정지 (paused_budget)</p>
        </div>
        <StatTile label="콘텐츠" value={`${contents.length}건`} sub={pending ? `검수 전 ${pending}건` : "모두 게이트 판정 완료"} icon="film" />
        <StatTile label="게시" value={`${published} / ${pubs.length}`} sub="PUBLISHED / 전체 게시 건" icon="send" />
      </div>

      <Card>
        <CardBody className="flex flex-wrap items-center gap-x-8 gap-y-3 pt-4">
          <DefList
            items={[
              ["실행 ID", <span key="id" className="font-mono text-[13px]">{run.id}</span>],
              ["시작 (KST)", kst(run.started_at)],
              ["종료 (KST)", run.ended_at ? kst(run.ended_at) : "—"],
              ["소요", run.started_at && runHasDuration(run.status) ? elapsed(run.started_at, run.ended_at, now) : "—"],
            ]}
          />
          <div className="flex flex-wrap gap-2" aria-label="게이트 판정 분포">
            {VERDICTS.map((v) => (
              <Pill key={v} tone={VERDICT[v].tone} dot>
                {v} {verdictCounts[v]}
              </Pill>
            ))}
            {pending ? <Pill tone="neutral">검수 전 {pending}</Pill> : null}
          </div>
        </CardBody>
      </Card>

      {run.plan ? (
        run.status === "awaiting_approval" ? (
          <PlanPreview plan={run.plan} status={run.status} runId={run.id} />
        ) : (
          <details className="group rounded-xl border border-line bg-surface shadow-card">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-4 text-[15px] font-semibold text-ink [&::-webkit-details-marker]:hidden">
              <Icon name="chevronRight" size={16} className="transition-transform group-open:rotate-90" />
              실행 계획 보기
              <span className="text-[13px] font-normal text-muted">
                {run.plan.steps.length}단계 · 대상 {run.plan.targets.length}곳 · {run.plan.approval.mode === "auto" ? "자동 승인" : "콘솔 승인"}
              </span>
            </summary>
            <div className="border-t border-line p-2 sm:p-3">
              <PlanPreview plan={run.plan} status={run.status} runId={run.id} />
            </div>
          </details>
        )
      ) : null}

      <Card aria-labelledby="contents-title">
        <CardHeader id="contents-title" title={`콘텐츠 (${contents.length})`} description="상품별 생산물 · 게이트 판정 · 플랫폼별 게시 상태" />
        <CardBody>
          {contents.length ? (
            <div className="grid gap-3 2xl:grid-cols-2">
              {contents.map((c, i) => (
                <ContentCard key={c.id} content={c} index={i} />
              ))}
            </div>
          ) : (
            <EmptyState icon="film" title={run.status === "awaiting_approval" ? "승인 후 생산이 시작됩니다" : "생산된 콘텐츠가 없습니다"}>
              {run.status === "awaiting_approval"
                ? "계획을 승인하면 상품 선택 → 링크 → 대본 → 렌더 → 게이트 순서로 진행됩니다."
                : run.status === "rejected"
                  ? "반려된 실행입니다."
                  : "분석 명령이거나 아직 콘텐츠가 만들어지지 않았습니다."}
            </EmptyState>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
