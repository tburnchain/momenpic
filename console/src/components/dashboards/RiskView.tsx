"use client";

import { Donut } from "@/components/charts/parts";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, Skeleton } from "@/components/ui/Feedback";
import { Icon } from "@/components/ui/Icon";
import { StatTile } from "@/components/ui/misc";
import { AccountStatusPill, Pill, PlatformTag } from "@/components/ui/Pill";
import { int, kst, relative } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import { VERDICT } from "@/lib/labels";
import { useRisk } from "@/lib/queries";
import { VERDICTS } from "@/lib/schemas";
import { VERDICT_COLOR } from "./PipelineView";

function scoreTone(score: number | null | undefined) {
  if (score === null || score === undefined) return "neutral" as const;
  return score >= 0.8 ? ("critical" as const) : score >= 0.6 ? ("serious" as const) : ("warn" as const);
}

export function RiskView() {
  const q = useRisk();
  const now = useNow();

  if (q.isPending) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-3 gap-3">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (q.error) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />;
  const d = q.data;
  const by = (s: string) => d.accounts.filter((a) => a.status === s).length;
  const order = { restricted: 0, cooldown: 1, active: 2 } as const;
  const accounts = [...d.accounts].sort((a, b) => order[a.status] - order[b.status]);

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        <StatTile label="활성 계정" value={int(by("active"))} icon="check" />
        <StatTile label="쿨다운" value={int(by("cooldown"))} sub="실패율 10%↑ · 도달률 급락 · 경고 시 48시간" icon="clock" tone={by("cooldown") ? "critical" : undefined} />
        <StatTile label="제한" value={int(by("restricted"))} sub="수동 조치 필요" icon="ban" tone={by("restricted") ? "critical" : undefined} />
      </div>

      <Card aria-labelledby="risk-accounts">
        <CardHeader id="risk-accounts" title="계정 상태" description="계정 = 브랜드. 계정별 토큰 버킷·쿨다운이 격리되어 있습니다 (§7.1)." />
        <CardBody>
          {accounts.length ? (
            <ul className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
              {accounts.map((a) => (
                <li
                  key={a.id}
                  className={`rounded-xl border p-3 ${
                    a.status === "restricted" ? "border-critical/40 bg-critical-soft" : a.status === "cooldown" ? "border-warn/50 bg-warn-soft" : "border-line bg-surface"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <PlatformTag platform={a.platform} className="font-medium" />
                    <AccountStatusPill status={a.status} />
                  </div>
                  <p className="mt-1 truncate text-sm text-ink">{a.handle}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    {a.cooldown_until ? `해제 ${relative(a.cooldown_until, now)} · ${kst(a.cooldown_until)}` : a.status === "active" ? "정상 게시 중" : "해제 예정 없음"}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState icon="users" title="등록된 계정이 없습니다" />
          )}
        </CardBody>
      </Card>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <Card aria-labelledby="risk-verdicts">
          <CardHeader id="risk-verdicts" title="게이트 판정 분포" description="PASS만 배포 · BLOCK은 배포 불가" />
          <CardBody>
            <Donut
              centerLabel="전체 판정"
              format={int}
              items={VERDICTS.map((v) => ({ key: v, label: `${v} ${VERDICT[v].label}`, value: d.gate_verdicts[v], color: VERDICT_COLOR[v] }))}
            />
          </CardBody>
        </Card>

        <Card aria-labelledby="risk-clicks">
          <CardHeader id="risk-clicks" title="무효 클릭 경보" description="동일 IP 반복·CTR 급등 감지 시 해당 링크 일시 중지 (M9). 무효 클릭 누적은 파트너스 이용 제한 사유." />
          <CardBody>
            {d.invalid_click_alerts.length ? (
              <ul className="divide-y divide-line">
                {d.invalid_click_alerts.map((a, i) => (
                  <li key={`${a.sub_id}-${i}`} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2.5">
                    <Pill tone={scoreTone(a.score)} icon="alert">
                      점수 {a.score !== null && a.score !== undefined ? a.score.toFixed(2) : "—"}
                    </Pill>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-ink">{a.reason}</p>
                      <p className="font-mono text-xs text-muted">{a.sub_id}</p>
                    </div>
                    <span className="text-xs tabular-nums text-muted" title={kst(a.at)}>
                      {relative(a.at, now)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon="shield" title="무효 클릭 경보 없음" />
            )}
          </CardBody>
        </Card>
      </div>

      <Card aria-labelledby="risk-warnings">
        <CardHeader id="risk-warnings" title="계정 경고" description="플랫폼 경고 응답 · 토큰 만료 · 쿨다운 전환 기록" />
        <CardBody>
          {d.warnings.length ? (
            <ul className="divide-y divide-line">
              {d.warnings.map((w, i) => (
                <li key={i} className="flex items-start gap-3 py-2.5 text-sm">
                  <Icon name="alert" size={16} className="mt-0.5 text-serious-ink" />
                  <div className="min-w-0 flex-1">
                    <span className="font-medium text-ink">{w.account}</span> <span className="text-ink-2">{w.detail}</span>
                  </div>
                  <span className="shrink-0 text-xs tabular-nums text-muted">{kst(w.at)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState icon="check" title="경고 없음" />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
