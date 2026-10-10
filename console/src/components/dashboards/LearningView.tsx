"use client";

import { RangeBar, RatioBar } from "@/components/charts/parts";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, Skeleton } from "@/components/ui/Feedback";
import { Pill, PlatformTag } from "@/components/ui/Pill";
import { int, krwPrecise, pct, pctSigned, topPercent, views } from "@/lib/format";
import { platformColor } from "@/lib/labels";
import { useLearning } from "@/lib/queries";

const HOOK_LABEL: Record<string, string> = {
  question: "질문",
  shock: "충격",
  before_after: "전후비교",
  price_twist: "가격반전",
  empathy: "공감",
};

const EXPLORE_TARGET = 0.2; // §4 M10: exploration budget 20 %

function ExploreExploit({ explore, exploit }: { explore: number; exploit: number }) {
  if (explore + exploit <= 0) {
    return <p className="text-sm text-muted">아직 M10 할당 기록이 없습니다. 목표 탐색 예산은 20%입니다.</p>;
  }
  const total = explore + exploit;
  const e = explore / total;
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span>
          <span className="text-muted">탐색</span> <strong className="tabular-nums">{pct(e, 0)}</strong>
        </span>
        <span>
          <span className="text-muted">활용</span> <strong className="tabular-nums">{pct(1 - e, 0)}</strong>
        </span>
      </div>
      <div className="relative mt-2">
        <div className="flex h-4 gap-[2px] overflow-hidden rounded-[4px]" role="img" aria-label={`탐색 ${pct(e, 0)}, 활용 ${pct(1 - e, 0)}, 목표 탐색 예산 20%`}>
          <div style={{ width: `${e * 100}%`, background: "var(--s2)" }} />
          <div style={{ width: `${(1 - e) * 100}%`, background: "var(--s1)" }} />
        </div>
        <div className="absolute -inset-y-1 w-px bg-ink" style={{ left: `${EXPLORE_TARGET * 100}%` }} title="탐색 예산 목표 20%" />
      </div>
      <p className="mt-2 text-xs text-muted">세로선 = 탐색 예산 목표 20% (Thompson Sampling)</p>
    </div>
  );
}

export function LearningView() {
  const q = useLearning();
  if (q.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-32" />
        <Skeleton className="h-80" />
      </div>
    );
  }
  if (q.error) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />;
  const d = q.data;

  const arms = [...d.arms].sort(
    (a, b) => Number(a.retired) - Number(b.retired) || (b.rpi_mean ?? 0) - (a.rpi_mean ?? 0) || (b.ctr_mean ?? 0) - (a.ctr_mean ?? 0),
  );
  const ctrMax = Math.max(0.001, ...arms.map((a) => a.ctr_ci?.[1] ?? a.ctr_mean ?? 0)) * 1.05;
  const ciLevel = arms[0]?.ci_level ?? 0.95;
  const admission = Object.entries(d.admission);

  return (
    <div className="space-y-5">
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card aria-labelledby="learn-ee" className="self-start">
          <CardHeader id="learn-ee" title="탐색 / 활용 비율" />
          <CardBody>
            <ExploreExploit explore={d.explore_exploit.explore} exploit={d.explore_exploit.exploit} />
          </CardBody>
        </Card>
        <Card aria-labelledby="learn-g3">
          <CardHeader id="learn-g3" title="G3 플랫폼 입장" description="기대 EPC가 I* 기준을 넘는 플랫폼에만 신규 콘텐츠를 배포합니다 (§7.2)." />
          <CardBody>
            {admission.length ? (
              <ul className="grid gap-2.5 sm:grid-cols-2">
                {admission.map(([platform, a]) => (
                  <li key={platform} className="rounded-xl border border-line p-3">
                    <div className="flex items-center justify-between gap-2">
                      <PlatformTag platform={platform} className="font-medium" />
                      {a.admitted ? (
                        <Pill tone="good" icon="check">
                          입장
                        </Pill>
                      ) : (
                        <Pill tone="neutral" icon="pause">
                          보류
                        </Pill>
                      )}
                    </div>
                    <p className="mt-1.5 text-xs text-muted">
                      중앙값 {views(a.median_views)} · I* {views(a.breakeven_views)}
                    </p>
                    <div className="mt-1.5">
                      <RatioBar ratio={a.ratio} />
                    </div>
                    {a.reason ? <p className="mt-1.5 text-xs text-ink-2">{a.reason}</p> : null}
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon="flask" title="입장 판정 데이터 없음" />
            )}
          </CardBody>
        </Card>
      </div>

      <Card aria-labelledby="learn-arms">
        <CardHeader
          id="learn-arms"
          title="Arm 사후분포"
          description="arm = (카테고리, 훅, 템플릿, 페르소나, 플랫폼, 시간대). CTR은 Beta-Bernoulli 사후 평균과 신용구간. 30회 시도 후 EPC 하위 10%는 퇴출."
        />
        <CardBody>
          {arms.length ? (
            <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
              <table className="w-full min-w-[860px] text-left text-sm">
                <caption className="sr-only">밴딧 arm별 사후분포</caption>
                <thead className="border-b border-line text-[13px] text-muted">
                  <tr>
                    <th scope="col" className="py-2 pr-3 font-medium">Arm</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">시도</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">CTR</th>
                    <th scope="col" className="w-56 py-2 pr-3 font-medium">
                      {Math.round(ciLevel * 100)}% 구간 <span className="font-normal">(0–{pct(ctrMax, 1)})</span>
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">EPC</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">RPI</th>
                    <th scope="col" className="py-2 font-medium">상태</th>
                  </tr>
                </thead>
                <tbody>
                  {arms.map((a) => (
                    <tr key={a.key} className={`border-b border-line last:border-0 ${a.retired ? "opacity-55" : ""}`}>
                      <td className="py-2.5 pr-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                          {a.platform ? <PlatformTag platform={a.platform} className="text-[13px]" /> : null}
                          <span className="text-ink">
                            {a.category} · {HOOK_LABEL[a.hook_type ?? ""] ?? a.hook_type}
                          </span>
                        </div>
                        <div className="text-xs text-muted">
                          {a.persona} · <span className="font-mono">{a.template}</span>
                          {a.slot !== null && a.slot !== undefined ? ` · ${a.slot}시` : ""}
                        </div>
                      </td>
                      <td className="py-2.5 pr-3 text-right tabular-nums">{int(a.trials)}</td>
                      <td className="py-2.5 pr-3 text-right font-medium tabular-nums">{pct(a.ctr_mean, 2)}</td>
                      <td className="py-2.5 pr-3">
                        {a.ctr_ci && a.ctr_ci[0] !== null && a.ctr_ci[1] !== null && a.ctr_mean !== null && a.ctr_mean !== undefined ? (
                          <RangeBar
                            lo={a.ctr_ci[0]}
                            hi={a.ctr_ci[1]}
                            mean={a.ctr_mean}
                            domain={[0, ctrMax]}
                            color={platformColor(a.platform)}
                            label={`CTR ${pct(a.ctr_mean, 2)}, ${Math.round(a.ci_level * 100)}% 구간 ${pct(a.ctr_ci[0], 2)}–${pct(a.ctr_ci[1], 2)}`}
                          />
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className="py-2.5 pr-3 text-right tabular-nums">{krwPrecise(a.epc_mean, 1)}</td>
                      <td className="py-2.5 pr-3 text-right tabular-nums">{krwPrecise(a.rpi_mean, 3)}</td>
                      <td className="py-2.5">
                        {a.retired ? (
                          <Pill tone="neutral" icon="ban">
                            퇴출
                          </Pill>
                        ) : (
                          <Pill tone="info" dot>
                            활성
                          </Pill>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState icon="flask" title="학습 중인 arm이 없습니다" />
          )}
        </CardBody>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card aria-labelledby="learn-tail">
          <CardHeader id="learn-tail" title="꼬리 콘텐츠" description="상위 1~5% 성과물 — 같은 상품·훅으로 변형 5개 생산 + 미게시 플랫폼 교차 배포 (증폭)" />
          <CardBody>
            {d.tail_contents.length ? (
              <ul className="divide-y divide-line">
                {d.tail_contents.map((t) => (
                  <li key={`${t.content_id}-${t.platform}`} className="flex items-start gap-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-ink">{t.hook ? `“${t.hook}”` : t.content_id}</p>
                      <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
                        <PlatformTag platform={t.platform} className="text-xs" />
                        <span className="font-mono">{t.content_id}</span>
                      </p>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="text-sm font-semibold tabular-nums">{views(t.views)}</p>
                      <p className="text-xs text-muted">{t.percentile !== null && t.percentile !== undefined ? topPercent(t.percentile) : "—"}</p>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon="chart" title="아직 꼬리 콘텐츠가 없습니다" />
            )}
          </CardBody>
        </Card>

        <Card aria-labelledby="learn-playbook">
          <CardHeader id="learn-playbook" title="플레이북 규칙" description="학습 결과를 자연어 규칙으로 요약해 M4 프롬프트에 주입 (RAG)" />
          <CardBody>
            {d.playbook.length ? (
              <ul className="space-y-2.5">
                {d.playbook.map((p, i) => (
                  <li key={i} className="rounded-lg border border-line px-3 py-2.5">
                    <p className="text-sm text-ink">{p.text}</p>
                    <p className="mt-1 flex flex-wrap gap-2 text-xs">
                      {p.confidence !== null && p.confidence !== undefined ? <Pill tone="neutral">신뢰도 {pct(p.confidence, 0)}</Pill> : null}
                      {p.lift !== null && p.lift !== undefined ? (
                        <Pill tone={p.lift >= 0 ? "good" : "critical"} icon={p.lift >= 0 ? "chevronRight" : "ban"}>
                          효과 {pctSigned(p.lift, 0)}
                        </Pill>
                      ) : null}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon="scroll" title="플레이북 규칙이 없습니다" />
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
