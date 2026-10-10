/** Small scale helpers for the hand-rolled SVG charts. */

export function niceStep(range: number, count: number): number {
  if (!(range > 0)) return 1;
  const raw = range / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return step * mag;
}

/** Rounded tick values covering [min, max] (always includes 0 when the range crosses it). */
export function niceTicks(min: number, max: number, count = 4): number[] {
  let lo = Math.min(min, max);
  let hi = Math.max(min, max);
  if (lo === hi) {
    hi = lo === 0 ? 1 : lo + Math.abs(lo) * 0.5;
    lo = Math.min(0, lo);
  }
  const step = niceStep(hi - lo, count);
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
  return ticks;
}

export function linear(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

/** Approximate rendered width of a tick label at 11px (Latin digits ~6.4px, Hangul ~11px). */
export function textWidth(s: string, px = 11): number {
  let w = 0;
  for (const ch of s) w += /[ㄱ-힝]/.test(ch) ? px : px * 0.6;
  return w;
}
