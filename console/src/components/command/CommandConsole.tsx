"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, Kbd, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { RunStatusPill } from "@/components/ui/Pill";
import { elapsed, krw, relative } from "@/lib/format";
import { runHasDuration, summaryText } from "@/lib/labels";
import { useNow } from "@/lib/hooks";
import { useRun, useRuns, useSubmitCommand, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { CommandResponse } from "@/lib/schemas";
import { PlanPreview } from "./PlanPreview";

/** Example commands: appendix A plus the slot examples of §5.2–5.3. */
const EXAMPLES: Array<{ label: string; text: string }> = [
  {
    label: "부록 A · 주방 쇼츠 5개",
    text: "주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘",
  },
  { label: "매일 9시 차량용품 10개", text: "매일 오전 9시에 차량용품 10개 글로 만들어서 스레드에 올려줘" },
  { label: "정리수납 3개 만들기만", text: "정리수납 3개 글로 만들기만 해줘" },
  { label: "오늘 밤 · 2천 원 이내", text: "주방 5개 쇼츠로 오늘 밤 유튜브랑 페이스북에 2천 원 이내로 올려줘" },
  { label: "성과 분석", text: "주방 카테고리 지난주 성과 분석해줘" },
];

const SLOTS = ["intent", "category", "count", "kind", "persona", "platforms", "schedule", "budget"];

function CommandResult({ result }: { result: CommandResponse }) {
  const live = useRun(result.run_id);
  const status = live.data?.run.status ?? result.status;
  if (!result.plan) {
    return (
      <Card>
        <CardBody className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-ink">계획을 만들지 못했습니다</span>
            <RunStatusPill status={status} />
          </div>
          {result.explanation ? <p className="text-sm text-ink-2">{result.explanation}</p> : null}
          <ButtonLink href={`/runs/${encodeURIComponent(result.run_id)}`} size="sm" icon="play">
            실행 상세
          </ButtonLink>
        </CardBody>
      </Card>
    );
  }
  return (
    <PlanPreview
      plan={result.plan}
      status={status}
      runId={result.run_id}
      explanation={result.explanation}
      headerExtra={
        <ButtonLink href={`/runs/${encodeURIComponent(result.run_id)}`} size="sm" icon="play">
          실행 상세
        </ButtonLink>
      }
    />
  );
}

export function CommandConsole() {
  const { can, role, query: meQuery } = usePrincipal();
  const submit = useSubmitCommand();
  const [text, setText] = useState("");
  const [result, setResult] = useState<CommandResponse | null>(null);
  const inputId = useId();
  const allowed = can("command");
  const trimmed = text.trim();

  const run = () => {
    if (!allowed || trimmed.length < 2 || submit.isPending) return;
    submit.mutate(trimmed, { onSuccess: (r) => setResult(r) });
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardBody className="space-y-4 pt-4 sm:pt-5">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              run();
            }}
            className="space-y-3"
          >
            <label htmlFor={inputId} className="block text-sm font-semibold text-ink">
              자연어 명령
            </label>
            <div className="relative">
              <textarea
                id={inputId}
                rows={3}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    run();
                  }
                }}
                disabled={!allowed}
                maxLength={500}
                aria-describedby={`${inputId}-help`}
                placeholder="예) 주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘"
                className="w-full resize-y rounded-xl border border-line-strong bg-surface px-4 py-3 text-base leading-relaxed text-ink placeholder:text-muted focus:border-accent focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-0 disabled:cursor-not-allowed disabled:opacity-60 sm:text-lg"
              />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p id={`${inputId}-help`} className="text-xs text-muted">
                슬롯 {SLOTS.map((s) => <code key={s} className="mx-0.5 rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px]">{s}</code>)} 를 해석해
                계획(DAG)을 만듭니다. 실행은 승인 후 시작됩니다. <Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd> 실행
              </p>
              <Button type="submit" variant="primary" size="lg" icon="terminal" loading={submit.isPending} disabled={!allowed || trimmed.length < 2}>
                계획 만들기
              </Button>
            </div>
          </form>

          <div>
            <p className="mb-2 text-xs font-medium text-muted">예시 명령</p>
            <div className="flex flex-wrap gap-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex.label}
                  type="button"
                  disabled={!allowed}
                  onClick={() => setText(ex.text)}
                  title={ex.text}
                  className="rounded-full border border-line-strong bg-surface px-3 py-1.5 text-[13px] text-ink-2 transition-colors hover:border-accent hover:text-accent-ink disabled:opacity-50"
                >
                  {ex.label}
                </button>
              ))}
            </div>
          </div>

          {!allowed && !meQuery.isPending ? (
            <Notice tone="warn" title="명령 권한 없음">
              자연어 명령은 {ALLOWED_ROLES.command}만 실행할 수 있습니다{role ? ` (현재 역할: ${role})` : ""}. 실행 목록과 계획은 열람할 수 있습니다.
            </Notice>
          ) : null}
          {submit.error ? <ErrorBanner error={submit.error} onRetry={run} retrying={submit.isPending} /> : null}
        </CardBody>
      </Card>

      {submit.isPending && !result ? (
        <Card>
          <CardBody className="pt-5">
            <p className="mb-3 flex items-center gap-2 text-sm text-muted">
              <Icon name="clock" size={15} /> Commander가 슬롯을 해석하고 계획을 만드는 중… (p95 20초)
            </p>
            <LoadingBlock rows={5} label="계획 생성 중" />
          </CardBody>
        </Card>
      ) : null}

      {result ? <CommandResult key={result.run_id} result={result} /> : null}

      <RecentRuns />
    </div>
  );
}

function RecentRuns() {
  const runs = useRuns(50);
  const now = useNow();
  const items = runs.data?.items.slice(0, 8) ?? [];
  return (
    <Card aria-labelledby="recent-runs">
      <CardHeader
        id="recent-runs"
        title="최근 실행"
        description="명령 → 계획 → 승인 → 생산 → 게이트 → 배포"
        actions={
          <ButtonLink href="/runs" size="sm" variant="ghost">
            모두 보기 <Icon name="chevronRight" size={14} />
          </ButtonLink>
        }
      />
      <CardBody>
        {runs.isPending ? (
          <LoadingBlock rows={5} />
        ) : runs.error ? (
          <ErrorBanner error={runs.error} onRetry={() => void runs.refetch()} retrying={runs.isFetching} />
        ) : !items.length ? (
          <EmptyState icon="play" title="아직 실행이 없습니다">
            위 명령창에 첫 명령을 입력해 보세요.
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {items.map((r) => (
              <li key={r.id}>
                <Link
                  href={`/runs/${encodeURIComponent(r.id)}`}
                  className="-mx-2 flex flex-col gap-1.5 rounded-lg px-2 py-3 transition-colors hover:bg-surface-2 sm:flex-row sm:items-center sm:gap-4"
                >
                  <span className="w-28 shrink-0">
                    <RunStatusPill status={r.status} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="line-clamp-2 text-sm text-ink sm:line-clamp-1">{r.raw_command}</span>
                    {summaryText(r.summary) ? <span className="mt-0.5 block truncate text-xs text-muted">{summaryText(r.summary)}</span> : null}
                  </span>
                  <span className="flex shrink-0 gap-3 text-xs text-muted tabular-nums sm:w-56 sm:justify-end">
                    <span>{r.cost_krw !== null && r.cost_krw !== undefined ? krw(r.cost_krw) : `추정 ${krw(r.est_cost_krw)}`}</span>
                    <span>{r.started_at ? relative(r.started_at, now) : "시작 전"}</span>
                    {r.started_at && runHasDuration(r.status) ? <span className="hidden md:inline">{elapsed(r.started_at, r.ended_at, now)}</span> : null}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
