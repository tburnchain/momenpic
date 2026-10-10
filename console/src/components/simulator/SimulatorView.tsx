"use client";

import { useId, useMemo, useState } from "react";
import { LineChart } from "@/components/charts/LineChart";
import { Legend, RatioBar } from "@/components/charts/parts";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorBanner, Skeleton } from "@/components/ui/Feedback";
import { PageHeader, StatTile } from "@/components/ui/misc";
import { int, krw, krwPrecise, pct, views } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { probTailHit } from "@/lib/quant";
import { useBreakeven, useSimulate } from "@/lib/queries";

interface Params {
  c_unit: number;
  ctr: number;
  cvr: number;
  aov: number;
  r: number;
  median: number;
  sigma: number;
  n: number;
}

/** §2.5 example assumptions (replace with measured values). */
const DEFAULTS: Params = { c_unit: 150, ctr: 0.008, cvr: 0.03, aov: 30000, r: 0.03, median: 600, sigma: 2, n: 300 };

interface SliderDef {
  key: keyof Params;
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  format: (v: number) => string;
}

const SLIDERS: SliderDef[] = [
  { key: "c_unit", label: "C_unit · 콘텐츠 1건 변동비", hint: "LLM + TTS + 렌더", min: 30, max: 1000, step: 10, format: krw },
  { key: "ctr", label: "CTR · 게시물 → 링크", hint: "플랫폼·링크 위치별", min: 0.001, max: 0.03, step: 0.0005, format: (v) => pct(v, 2) },
  { key: "cvr", label: "CVR · 쿠팡 랜딩 → 구매", hint: "상품 선별(M1)", min: 0.005, max: 0.1, step: 0.001, format: (v) => pct(v, 1) },
  { key: "aov", label: "AOV · 평균 주문금액", hint: "동반구매 포함", min: 5000, max: 150000, step: 1000, format: krw },
  { key: "r", label: "r · 수수료율", hint: "카테고리별 차등", min: 0.01, max: 0.1, step: 0.001, format: (v) => pct(v, 1) },
  { key: "median", label: "중앙값 조회수", hint: "채널 실측 중앙값", min: 50, max: 10000, step: 50, format: (v) => views(v) },
  { key: "sigma", label: "σ · 로그정규 분산", hint: "클수록 꼬리가 두꺼움", min: 0.5, max: 3, step: 0.1, format: (v) => v.toFixed(1) },
  { key: "n", label: "n · 시도 횟수", hint: "콘텐츠 생산 수", min: 10, max: 1000, step: 10, format: (v) => `${int(v)}건` },
];

function Slider({ def, value, onChange }: { def: SliderDef; value: number; onChange: (v: number) => void }) {
  const id = useId();
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="text-[13px] font-medium text-ink-2">
          {def.label}
        </label>
        <output htmlFor={id} className="text-sm font-semibold tabular-nums text-ink">
          {def.format(value)}
        </output>
      </div>
      <input
        id={id}
        type="range"
        min={def.min}
        max={def.max}
        step={def.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-valuetext={def.format(value)}
        className="mt-1.5 h-6 w-full cursor-pointer"
      />
      <p className="text-[11px] text-muted">{def.hint}</p>
    </div>
  );
}

const CURVE_Q = [
  { q: 0.005, label: "상위 0.5%", color: "var(--s1)" },
  { q: 0.01, label: "상위 1%", color: "var(--s2)" },
  { q: 0.02, label: "상위 2%", color: "var(--s3)" },
];

export function SimulatorView() {
  const [p, setP] = useState<Params>(DEFAULTS);
  const dp = useDebounced(p, 300);
  const funnel = { c_unit: dp.c_unit, ctr: dp.ctr, cvr: dp.cvr, aov: dp.aov, r: dp.r };
  const be = useBreakeven(funnel);
  const sim = useSimulate(dp);
  const stale = be.isPlaceholderData || sim.isPlaceholderData || p !== dp;

  const curve = useMemo(() => {
    const maxN = Math.max(1000, Math.ceil((p.n * 1.5) / 100) * 100);
    const steps = 100;
    const ns = Array.from({ length: steps + 1 }, (_, i) => Math.round((i * maxN) / steps));
    const marker = Math.round((p.n / maxN) * steps);
    return { ns, marker, series: CURVE_Q.map((c) => ({ key: String(c.q), label: c.label, color: c.color, values: ns.map((n) => probTailHit(c.q, n)) })) };
  }, [p.n]);

  const s = sim.data;
  const b = be.data;
  const meanMultiple = Math.exp((p.sigma * p.sigma) / 2);
  const ratio = b?.breakeven_views ? p.median / b.breakeven_views : null;

  return (
    <>
      <PageHeader
        title="수익 시뮬레이터"
        description="손익분기 조회수 I*와 로그정규 꼬리 분포로 ‘대량 생산’의 기대값을 계산합니다 (§2). 값은 /quant API가 계산합니다."
        actions={
          <Button icon="refresh" onClick={() => setP(DEFAULTS)}>
            §2.5 기본값
          </Button>
        }
      />
      <div className="grid gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <Card aria-labelledby="sim-inputs" className="h-fit xl:sticky xl:top-6">
          <CardHeader id="sim-inputs" title="가정" description="실측으로 교체하세요" />
          <CardBody className="space-y-4">
            {SLIDERS.map((d) => (
              <Slider key={d.key} def={d} value={p[d.key]} onChange={(v) => setP((prev) => ({ ...prev, [d.key]: v }))} />
            ))}
          </CardBody>
        </Card>

        <div className="min-w-0 space-y-5">
          {be.error || sim.error ? (
            <ErrorBanner
              error={be.error ?? sim.error}
              onRetry={() => {
                void be.refetch();
                void sim.refetch();
              }}
              retrying={be.isFetching || sim.isFetching}
            />
          ) : null}

          <div className={`grid grid-cols-2 gap-3 lg:grid-cols-4 transition-opacity ${stale ? "opacity-60" : ""}`} aria-busy={stale}>
            {b && s ? (
              <>
                <StatTile label="손익분기 조회수 I*" value={b.breakeven_views === null ? "∞" : views(b.breakeven_views)} sub="C_unit ÷ (CTR·CVR·AOV·r)" />
                <StatTile label="P(I ≥ I*)" value={pct(s.p_breakeven, 1)} sub="콘텐츠 1건이 본전을 넘을 확률" />
                <StatTile
                  label="콘텐츠당 기대 이익"
                  value={krwPrecise(s.expected_profit_per_content, 0)}
                  sub="E[I]·RPI − C_unit"
                  tone={s.expected_profit_per_content >= 0 ? "good" : "critical"}
                />
                <StatTile label="평균 조회수 E[I]" value={views(s.mean_views)} sub={`중앙값의 ${meanMultiple.toFixed(1)}배 (e^{σ²/2})`} />
                <StatTile label="상위 1% 조회 점유율" value={pct(s.tail_share_top1, 1)} sub="전체 조회수 중 상위 1% 몫" />
                <StatTile label="상위 5% 조회 점유율" value={pct(s.tail_share_top5, 1)} sub="Φ(σ − z₀.₉₅)" />
                <StatTile
                  label={`P(n=${int(s.prob_tail_hit.n)}에서 상위 ${pct(s.prob_tail_hit.q, 0)} ≥ 1)`}
                  value={pct(s.prob_tail_hit.p, 1)}
                  sub="1 − (1 − q)^n"
                />
                <StatTile label="95% 도달 시도 수" value={s.trials_for_95 === null ? "—" : `${int(s.trials_for_95)}건`} sub={`q = ${pct(s.prob_tail_hit.q, 0)}`} />
              </>
            ) : (
              Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-24" />)
            )}
          </div>

          <Card aria-labelledby="sim-funnel">
            <CardHeader id="sim-funnel" title="퍼널 단가" description="E[R] = I · CTR · CVR · AOV · r" />
            <CardBody className={`grid gap-4 sm:grid-cols-[repeat(3,minmax(0,1fr))_minmax(0,1.4fr)] ${stale ? "opacity-60" : ""}`}>
              <div>
                <p className="text-[13px] text-muted">주문 1건당 수수료</p>
                <p className="text-lg font-semibold tabular-nums">{b ? krwPrecise(b.commission_per_order, 0) : "—"}</p>
              </div>
              <div>
                <p className="text-[13px] text-muted">EPC (클릭당 수익)</p>
                <p className="text-lg font-semibold tabular-nums">{b ? krwPrecise(b.epc, 1) : "—"}</p>
              </div>
              <div>
                <p className="text-[13px] text-muted">RPI (조회당 수익)</p>
                <p className="text-lg font-semibold tabular-nums">{b ? krwPrecise(b.revenue_per_impression, 3) : "—"}</p>
              </div>
              <div>
                <p className="text-[13px] text-muted">중앙값 ÷ I* (KPI 1순위)</p>
                <div className="mt-1.5">
                  <RatioBar ratio={ratio} />
                </div>
              </div>
            </CardBody>
          </Card>

          <Card aria-labelledby="sim-curve">
            <CardHeader id="sim-curve" title="꼬리를 만날 확률 1 − (1 − q)ⁿ" description="n번 시도에서 상위 q 분위 콘텐츠를 1개 이상 얻을 확률 — C_unit을 낮출수록 n을 늘릴 수 있습니다" />
            <CardBody>
              <Legend className="mb-3" items={curve.series.map((x) => ({ key: x.key, label: x.label, color: x.color }))} />
              <LineChart
                ariaLabel="시도 횟수 n에 따른 상위 q 콘텐츠 적중 확률 곡선"
                x={curve.ns.map((n) => String(n))}
                xTitle={(i) => `n = ${int(curve.ns[i])}`}
                series={curve.series}
                yFormat={(v) => pct(v, 1)}
                yTickFormat={(v) => pct(v, 0)}
                yDomain={[0, 1]}
                refLines={[{ y: 0.95, label: "95%" }]}
                vMarkers={[{ index: curve.marker, label: `현재 n = ${int(p.n)}` }]}
                xTickEvery={10}
                height={260}
              />
            </CardBody>
          </Card>

          <Card aria-labelledby="sim-explain">
            <CardHeader id="sim-explain" title="로그정규 꼬리의 수학" />
            <CardBody>
              <p className="text-[15px] leading-relaxed text-ink">
                숏폼 조회수 I는 대략 로그정규분포(ln I ~ N(μ, σ²))를 따르므로 평균은 중앙값보다 훨씬 큽니다: E[I] = 중앙값 · e<sup>σ²/2</sup>, σ = 2이면 약 7.4배입니다. 콘텐츠
                1건이 손익분기 I* = C_unit ÷ (CTR · CVR · AOV · r)을 넘을 확률은 P(I ≥ I*) = 1 − Φ((ln I* − ln 중앙값) / σ)로, 대부분의 콘텐츠는 손해를 보지만 상위 q
                분위가 차지하는 조회수 비중 Φ(σ − z<sub>1−q</sub>)는 σ가 클수록 급격히 커져 상위 1~5%가 전체 수익의 대부분을 만듭니다. 그래서 목적함수는 &lsquo;평균
                콘텐츠&rsquo;가 아니라 한계비용 C_unit을 낮춰 시도 횟수 n을 늘리고(n번 중 상위 q를 1개 이상 만날 확률 1 − (1 − q)<sup>n</sup>, q = 1%면 n = 100에서 63%, n =
                300에서 95%), 꼬리를 만나면 같은 상품·훅으로 즉시 증폭하는 것입니다.
              </p>
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}
