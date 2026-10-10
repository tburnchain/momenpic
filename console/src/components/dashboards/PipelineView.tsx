"use client";

import { BarList, Legend } from "@/components/charts/parts";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorBanner, Skeleton } from "@/components/ui/Feedback";
import { StatTile } from "@/components/ui/misc";
import { int, pct, seconds } from "@/lib/format";
import { PUBLICATION_STATES, RUN_STATUSES, VERDICTS, type GateVerdicts } from "@/lib/schemas";
import { RUN_STATUS, VERDICT, publicationState, type Tone } from "@/lib/labels";
import { usePipeline } from "@/lib/queries";

const TONE_COLOR: Record<Tone, string> = {
  neutral: "var(--axis)",
  info: "var(--accent)",
  good: "var(--good)",
  warn: "var(--warn)",
  serious: "var(--serious)",
  critical: "var(--critical)",
};

export const VERDICT_COLOR: Record<string, string> = {
  PASS: "var(--good)",
  FIX: "var(--warn)",
  HOLD: "var(--serious)",
  BLOCK: "var(--critical)",
};

/** Single 100 % stacked bar for the four gate verdicts (2 px surface gaps between segments). */
export function VerdictStack({ verdicts }: { verdicts: GateVerdicts }) {
  const total = VERDICTS.reduce((a, v) => a + verdicts[v], 0);
  return (
    <div>
      <div className="flex h-4 w-full gap-[2px] overflow-hidden rounded-[4px]" role="img" aria-label={VERDICTS.map((v) => `${v} ${verdicts[v]}건`).join(", ")}>
        {total > 0
          ? VERDICTS.filter((v) => verdicts[v] > 0).map((v) => (
              <div key={v} style={{ width: `${(verdicts[v] / total) * 100}%`, background: VERDICT_COLOR[v] }} title={`${v} ${VERDICT[v].label}: ${int(verdicts[v])}건`} />
            ))
          : <div className="w-full bg-surface-2" />}
      </div>
      <Legend
        kind="box"
        className="mt-3"
        items={VERDICTS.map((v) => ({
          key: v,
          label: `${v} ${VERDICT[v].label}`,
          color: VERDICT_COLOR[v],
          value: `${int(verdicts[v])} (${total ? pct(verdicts[v] / total, 1) : "—"})`,
        }))}
      />
    </div>
  );
}

/** p50/p95 render time on a 0–90 s strip with the 60 s target (§4 M6: 30 s video ≤ 60 s CPU). */
const QUEUE_LABEL: Record<string, string> = {
  publish_due: "게시 시각 도래",
  publish_queued: "게시 대기 (QUEUED)",
  manual_pending: "수동 게시 대기",
  review_pending: "HOLD 검토 대기",
  outbox_pending: "이벤트 아웃박스",
};

function RenderStrip({ p50, p95 }: { p50: number | null | undefined; p95: number | null | undefined }) {
  const max = Math.max(90, (p95 ?? 0) * 1.1);
  const at = (v: number) => `${Math.min(100, (v / max) * 100)}%`;
  return (
    <div className="pt-6">
      <div className="relative h-3 rounded-full bg-surface-2" role="img" aria-label={`렌더 시간 p50 ${seconds(p50)}, p95 ${seconds(p95)}, 목표 60초`}>
        {p50 !== null && p50 !== undefined && p95 !== null && p95 !== undefined ? (
          <div className="absolute inset-y-0 rounded-full bg-accent/35" style={{ left: at(p50), width: `calc(${at(p95)} - ${at(p50)})` }} />
        ) : null}
        {[
          { v: p50, label: "p50" },
          { v: p95, label: "p95" },
        ].map((m) =>
          m.v !== null && m.v !== undefined ? (
            <div key={m.label} className="absolute -top-6 -translate-x-1/2 text-center" style={{ left: at(m.v) }}>
              <span className="block text-[11px] font-semibold tabular-nums text-ink">
                {m.label} {seconds(m.v)}
              </span>
              <span className="mx-auto mt-0.5 block h-[22px] w-0.5 bg-accent" />
            </div>
          ) : null,
        )}
        <div className="absolute -inset-y-1.5 w-px bg-critical" style={{ left: at(60) }} />
      </div>
      <div className="relative mt-2 h-4 text-[11px] tabular-nums text-muted">
        <span className="absolute left-0">0초</span>
        <span className="absolute -translate-x-1/2 text-critical-ink" style={{ left: at(60) }}>
          목표 60초
        </span>
        <span className="absolute right-0">{Math.round(max)}초</span>
      </div>
    </div>
  );
}

export function PipelineView() {
  const q = usePipeline();
  if (q.isPending) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (q.error) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />;
  const d = q.data;

  const stateKeys = Array.from(new Set([...PUBLICATION_STATES.filter((s) => s in d.publication_states), ...Object.keys(d.publication_states)]));
  const pubTotal = stateKeys.reduce((a, k) => a + (d.publication_states[k] ?? 0), 0);
  const failed = (d.publication_states.FAILED ?? 0) + (d.publication_states.EXPIRED ?? 0);
  const gateTotal = VERDICTS.reduce((a, v) => a + d.gate_verdicts[v], 0);
  const passable = d.gate_verdicts.PASS + d.gate_verdicts.FIX;
  const queueKeys = Object.keys(d.queues);
  const queueTotal = queueKeys.reduce((a, k) => a + d.queues[k], 0);
  const runKeys = Array.from(new Set([...RUN_STATUSES.filter((s) => s in d.runs_by_status), ...Object.keys(d.runs_by_status)]));

  return (
    <div className={`space-y-5 transition-opacity ${q.isFetching ? "opacity-90" : ""}`}>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {d.render.metric === "video_length" ? (
          <StatTile
            label="영상 길이 p50 / p90"
            value={`${seconds(d.render.p50_s)} / ${seconds(d.render.p90_s)}`}
            sub={`렌더 ${int(d.render.count)}건 · 목표 20~25초`}
            icon="film"
          />
        ) : (
          <StatTile label="렌더 p50 / p95" value={`${seconds(d.render.p50_s)} / ${seconds(d.render.p95_s)}`} sub={`렌더 ${int(d.render.count)}건 · 목표 60초 이내`} icon="film" />
        )}
        <StatTile label="게이트 통과율" value={gateTotal ? pct(passable / gateTotal, 1) : "—"} sub="(PASS + FIX) ÷ 전체 판정" icon="shield" />
        <StatTile
          label="게시 실패율"
          value={d.publish_failure_rate !== null && d.publish_failure_rate !== undefined ? pct(d.publish_failure_rate, 2) : pubTotal ? pct(failed / pubTotal, 2) : "—"}
          sub="FAILED + EXPIRED · SLO 성공률 99%"
          tone={pubTotal && failed / pubTotal > 0.01 ? "critical" : undefined}
          icon="send"
        />
        <StatTile label="대기열 합계" value={`${int(queueTotal)}건`} sub={`${queueKeys.length}개 큐`} icon="inbox" />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card aria-labelledby="pipe-states">
          <CardHeader id="pipe-states" title="게시 상태" description={`M8 상태 머신 · 전체 ${int(pubTotal)}건`} />
          <CardBody>
            <BarList
              ariaLabel="게시 상태별 건수"
              items={stateKeys.map((k) => {
                const s = publicationState(k);
                return { key: k, label: s.label, hint: k, value: d.publication_states[k] ?? 0, color: TONE_COLOR[s.tone] };
              })}
              format={int}
              labelWidth="11rem"
            />
          </CardBody>
        </Card>

        <Card aria-labelledby="pipe-render">
          {d.render.metric === "video_length" ? (
            <CardHeader id="pipe-render" title="렌더 영상 길이" description="M6 출력 규격 20~25초 · 빠른 보이스 8음절/초 (최근 24시간)" />
          ) : (
            <CardHeader id="pipe-render" title="렌더 시간 분포" description="M6 · 25초 영상 1건 CPU 렌더 60초 이내, 동시 8건" />
          )}
          <CardBody>
            {d.render.count === 0 ? (
              <p className="text-sm text-muted">최근 렌더가 없습니다.</p>
            ) : d.render.metric === "video_length" ? (
              <p className="text-sm text-ink">
                중앙값 <strong className="tabular-nums">{seconds(d.render.p50_s)}</strong> · 90분위 <strong className="tabular-nums">{seconds(d.render.p90_s)}</strong>{" "}
                <span className="text-muted">({int(d.render.count)}건)</span>
              </p>
            ) : (
              <RenderStrip p50={d.render.p50_s} p95={d.render.p95_s} />
            )}
            <div className="mt-6">
              <h3 className="mb-2 text-[13px] font-semibold text-ink-2">M7 게이트 판정</h3>
              <VerdictStack verdicts={d.gate_verdicts} />
            </div>
          </CardBody>
        </Card>

        <Card aria-labelledby="pipe-queues">
          <CardHeader id="pipe-queues" title="대기열 깊이" description="모듈별 이벤트 큐 (적체 시 해당 단계 확인)" />
          <CardBody>
            <BarList ariaLabel="큐별 대기 건수" items={queueKeys.map((k) => ({ key: k, label: QUEUE_LABEL[k] ?? <span className="font-mono text-xs">{k}</span>, hint: k, value: d.queues[k], color: "var(--s1)" }))} format={int} labelWidth="10rem" />
          </CardBody>
        </Card>

        <Card aria-labelledby="pipe-runs">
          <CardHeader id="pipe-runs" title="실행 상태" description="명령 실행(run) 상태별 건수" />
          <CardBody>
            <BarList
              ariaLabel="실행 상태별 건수"
              items={runKeys.map((k) => {
                const s = RUN_STATUS[k as keyof typeof RUN_STATUS];
                return { key: k, label: s ? s.label : k, value: d.runs_by_status[k] ?? 0, color: TONE_COLOR[s?.tone ?? "neutral"] };
              })}
              format={int}
            />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
