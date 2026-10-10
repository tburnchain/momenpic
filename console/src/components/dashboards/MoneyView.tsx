"use client";

import { useState } from "react";
import { LineChart } from "@/components/charts/LineChart";
import { ChartTable, Legend, RatioBar } from "@/components/charts/parts";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, Notice, Skeleton } from "@/components/ui/Feedback";
import { Segmented, StatTile } from "@/components/ui/misc";
import { Pill, PlatformTag } from "@/components/ui/Pill";
import { dayLabel, int, krw, krwCompact, pct, views } from "@/lib/format";
import { useMoney } from "@/lib/queries";

const SERIES = {
  commission: { label: "확정 수수료", color: "var(--s1)" },
  cost: { label: "비용 (C_unit 합)", color: "var(--s2)" },
  net: { label: "순이익", color: "var(--s3)" },
  cancelled: { label: "취소·반품 차감", color: "var(--s4)" },
  gmv: { label: "GMV", color: "var(--s5)" },
} as const;

export function MoneyView() {
  const [days, setDays] = useState(30);
  const q = useMoney(days);
  const d = q.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented
          label="기간"
          value={days}
          onChange={setDays}
          options={[
            { value: 7, label: "최근 7일" },
            { value: 30, label: "최근 30일" },
            { value: 90, label: "최근 90일" },
          ]}
        />
        {q.isFetching && d ? <span className="text-xs text-muted">갱신 중…</span> : null}
      </div>

      <Notice>
        GMV(상품 판매액) · 확정 수수료 · 순이익은 항상 분리해 표시합니다. GMV는 수익이 아니며, 단일 &ldquo;매출&rdquo; 숫자는 쓰지 않습니다 (§2.4).
      </Notice>

      {q.error && !d ? <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} /> : null}

      {!d ? (
        q.isPending ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
            <Skeleton className="h-72" />
          </div>
        ) : null
      ) : (
        <div className={`space-y-5 transition-opacity ${q.isPlaceholderData ? "opacity-60" : ""}`}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <StatTile label="GMV (상품 판매액)" value={krwCompact(d.totals.gmv_krw)} sub={`${krw(d.totals.gmv_krw)} · 수익 아님`} />
            <StatTile label="확정 수수료" value={krwCompact(d.totals.commission_krw)} sub={krw(d.totals.commission_krw)} />
            <StatTile label="취소·반품 차감" value={krwCompact(-d.totals.cancelled_krw)} sub={krw(-d.totals.cancelled_krw)} />
            <StatTile label="비용 (변동비)" value={krwCompact(-d.totals.cost_krw)} sub={krw(-d.totals.cost_krw)} />
            <StatTile
              label="순이익"
              value={krwCompact(d.totals.net_krw)}
              sub={`${krw(d.totals.net_krw)} = 수수료 − 취소 − 비용`}
              tone={d.totals.net_krw >= 0 ? "good" : "critical"}
            />
            <StatTile label="실효 수수료율" value={pct(d.totals.effective_rate, 2)} sub="확정 수수료 ÷ GMV" />
          </div>

          {d.daily.length && d.totals.gmv_krw === 0 && d.totals.commission_krw === 0 && d.totals.cost_krw === 0 ? (
            <Notice>아직 이 기간의 수익·비용 데이터가 없습니다. 쿠팡 파트너스 리포트는 D+1에 수집되고, 비용은 생산이 시작되면 쌓입니다.</Notice>
          ) : null}
          {d.daily.length ? (
            <div className="grid gap-5 xl:grid-cols-3">
              <Card className="xl:col-span-2" aria-labelledby="money-lines">
                <CardHeader id="money-lines" title="일별 수수료 · 비용 · 순이익" description="같은 원화 축 — 순이익이 0 아래면 그날은 손실" />
                <CardBody>
                  <Legend
                    className="mb-3"
                    items={(["commission", "cost", "net", "cancelled"] as const).map((k) => ({ key: k, label: SERIES[k].label, color: SERIES[k].color }))}
                  />
                  <LineChart
                    ariaLabel="일별 확정 수수료, 비용, 순이익, 취소 차감 추이"
                    x={d.daily.map((r) => dayLabel(r.date))}
                    xTitle={(i) => d.daily[i].date}
                    series={[
                      { key: "commission", ...SERIES.commission, values: d.daily.map((r) => r.commission_krw) },
                      { key: "cost", ...SERIES.cost, values: d.daily.map((r) => r.cost_krw) },
                      { key: "net", ...SERIES.net, values: d.daily.map((r) => r.net_krw) },
                      { key: "cancelled", ...SERIES.cancelled, values: d.daily.map((r) => r.cancelled_krw) },
                    ]}
                    yFormat={krw}
                    yTickFormat={krwCompact}
                    minSpan={10_000}
                    height={260}
                  />
                  <ChartTable
                    caption="일별 머니 지표"
                    columns={["날짜", "GMV", "확정 수수료", "취소 차감", "비용", "순이익"]}
                    rows={d.daily.map((r) => [r.date, krw(r.gmv_krw), krw(r.commission_krw), krw(-r.cancelled_krw), krw(-r.cost_krw), krw(r.net_krw)])}
                  />
                </CardBody>
              </Card>
              <Card aria-labelledby="money-gmv">
                <CardHeader id="money-gmv" title="일별 GMV" description="참고 지표 — 쿠팡 상품 판매액이며 우리 수익이 아닙니다" />
                <CardBody>
                  <LineChart
                    ariaLabel="일별 GMV 추이"
                    x={d.daily.map((r) => dayLabel(r.date))}
                    xTitle={(i) => d.daily[i].date}
                    series={[{ key: "gmv", ...SERIES.gmv, values: d.daily.map((r) => r.gmv_krw), area: true }]}
                    yFormat={krw}
                    yTickFormat={krwCompact}
                    minSpan={10_000}
                    height={260}
                  />
                </CardBody>
              </Card>
            </div>
          ) : (
            <Card>
              <EmptyState icon="chart" title="이 기간의 머니 데이터가 없습니다">
                쿠팡 파트너스 리포트는 D+1에 수집됩니다.
              </EmptyState>
            </Card>
          )}

          <Card aria-labelledby="money-channels">
            <CardHeader
              id="money-channels"
              title="채널별 중앙값 조회수 ÷ I*  (KPI 1순위)"
              description="1.0× 이상인 채널은 콘텐츠를 늘릴수록 이익이 늘고, 미만인 채널은 꼬리 콘텐츠가 손실을 메워야 합니다 (§2.3). G3 입장 = 신규 콘텐츠 배포 대상."
            />
            <CardBody>
              {d.channels.length ? (
                <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
                  <table className="w-full min-w-[720px] text-left text-sm">
                    <caption className="sr-only">채널별 중앙값 조회수와 손익분기 비율</caption>
                    <thead className="border-b border-line text-[13px] text-muted">
                      <tr>
                        <th scope="col" className="py-2 pr-3 font-medium">채널</th>
                        <th scope="col" className="py-2 pr-3 text-right font-medium">표본 n</th>
                        <th scope="col" className="py-2 pr-3 text-right font-medium">중앙값 조회 (95% CI)</th>
                        <th scope="col" className="py-2 pr-3 text-right font-medium">손익분기 I*</th>
                        <th scope="col" className="w-64 py-2 pr-3 font-medium">중앙값 / I*</th>
                        <th scope="col" className="py-2 font-medium">G3 입장</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...d.channels]
                        .sort((a, b) => (b.ratio ?? -1) - (a.ratio ?? -1))
                        .map((c) => (
                          <tr key={`${c.platform}-${c.account}`} className="border-b border-line last:border-0">
                            <td className="py-2.5 pr-3">
                              <PlatformTag platform={c.platform} />
                              <div className="text-xs text-muted">{c.account}</div>
                            </td>
                            <td className="py-2.5 pr-3 text-right tabular-nums">{int(c.n)}</td>
                            <td className="py-2.5 pr-3 text-right tabular-nums">
                              {views(c.median_views)}
                              {c.median_ci ? (
                                <div className="text-xs text-muted">
                                  {int(c.median_ci[0])}–{int(c.median_ci[1])}
                                </div>
                              ) : null}
                            </td>
                            <td className="py-2.5 pr-3 text-right tabular-nums">{views(c.breakeven_views)}</td>
                            <td className="py-2.5 pr-3">
                              <RatioBar ratio={c.ratio} />
                            </td>
                            <td className="py-2.5">
                              {c.admitted ? (
                                <Pill tone="good" icon="check">
                                  입장
                                </Pill>
                              ) : (
                                <Pill tone="neutral" icon="pause">
                                  보류
                                </Pill>
                              )}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState icon="chart" title="채널 데이터가 없습니다" />
              )}
              <p className="mt-3 text-xs text-muted">세로선 = 손익분기 1.0×. I* = C_unit ÷ (CTR · CVR · AOV · r).</p>
            </CardBody>
          </Card>
        </div>
      )}
    </div>
  );
}
