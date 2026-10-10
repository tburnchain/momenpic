/**
 * Closed-form revenue math (§2) used by the simulator curve and by mock mode.
 * The live simulator numbers come from the API (/quant/*); this module only draws the
 * 1 − (1 − q)^n curve and serves fixtures when NEXT_PUBLIC_ACVM_MOCK=1.
 */

/** Standard normal CDF (Abramowitz–Stegun 7.1.26 via erf, |ε| < 1.5e-7). */
export function normCdf(x: number): number {
  const z = Math.abs(x) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * z);
  const erf =
    1 -
    ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) *
      t *
      Math.exp(-z * z);
  return x >= 0 ? 0.5 * (1 + erf) : 0.5 * (1 - erf);
}

/** Inverse standard normal CDF (Acklam rational approximation). */
export function normInv(p: number): number {
  if (p <= 0) return -Infinity;
  if (p >= 1) return Infinity;
  const a = [-39.6968302866538, 220.946098424521, -275.928510446969, 138.357751867269, -30.6647980661472, 2.50662827745924];
  const b = [-54.4760987982241, 161.585836858041, -155.698979859887, 66.8013118877197, -13.2806815528857];
  const c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184, -2.54973253934373, 4.37466414146497, 2.93816398269878];
  const d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742];
  const lo = 0.02425;
  if (p < lo) {
    const q = Math.sqrt(-2 * Math.log(p));
    return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  if (p > 1 - lo) {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  const q = p - 0.5;
  const r = q * q;
  return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
}

/** P(at least one top-q content in n tries) = 1 − (1 − q)^n  (§2.2) */
export function probTailHit(q: number, n: number): number {
  return 1 - Math.pow(1 - q, n);
}

/** Smallest n with 1 − (1 − q)^n ≥ target. */
export function trialsFor(q: number, target = 0.95): number | null {
  if (q <= 0 || q >= 1) return null;
  return Math.ceil(Math.log(1 - target) / Math.log(1 - q));
}

export interface FunnelInput {
  c_unit: number;
  ctr: number;
  cvr: number;
  aov: number;
  r: number;
}

export function breakeven(f: FunnelInput) {
  const commission_per_order = f.aov * f.r;
  const epc = f.cvr * commission_per_order;
  const revenue_per_impression = f.ctr * epc;
  return {
    breakeven_views: revenue_per_impression > 0 ? f.c_unit / revenue_per_impression : null,
    revenue_per_impression,
    epc,
    commission_per_order,
  };
}

/** Analytic lognormal summary — mirrors what /quant/simulate returns. */
export function simulate(f: FunnelInput & { median: number; sigma: number; n: number }) {
  const be = breakeven(f);
  const { median, sigma, n } = f;
  const mean_views = median * Math.exp((sigma * sigma) / 2);
  const p_breakeven =
    be.breakeven_views === null ? 0 : 1 - normCdf((Math.log(be.breakeven_views) - Math.log(median)) / sigma);
  // Share of all views produced by the top-q fraction of contents: Φ(σ − z_{1−q}).
  const tailShare = (q: number) => normCdf(sigma - normInv(1 - q));
  const q = 0.01;
  return {
    mean_views,
    p_breakeven,
    expected_profit_per_content: mean_views * be.revenue_per_impression - f.c_unit,
    tail_share_top1: tailShare(0.01),
    tail_share_top5: tailShare(0.05),
    prob_tail_hit: { q, n, p: probTailHit(q, n) },
    trials_for_95: trialsFor(q, 0.95),
  };
}
