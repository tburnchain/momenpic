/**
 * Display formatting. All money is KRW (₩, thousands separators, no decimals); all times are
 * stored UTC and rendered in KST (Asia/Seoul).
 */
const TZ = "Asia/Seoul";
export const DASH = "—";

const intFmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });

function finite(n: number | null | undefined): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

/** ₩1,234,567 · -₩12,000 */
export function krw(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  const v = Math.round(n);
  return `${v < 0 ? "-" : ""}₩${intFmt.format(Math.abs(v))}`;
}

/** Signed KRW for deltas: +₩1,200 / -₩300 */
export function krwSigned(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  return n > 0 ? `+${krw(n)}` : krw(n);
}

/** ₩1.2만 · ₩3.4억 — for chart axes and dense tiles. */
export function krwCompact(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  const v = Math.round(n);
  return `${v < 0 ? "-" : ""}₩${koUnits(Math.abs(v))}`;
}

/** Korean myriad units: 8,900 · 19.2만 · 9,623만 · 1.23억 */
function koUnits(abs: number): string {
  if (abs < 10_000) return intFmt.format(abs);
  if (abs < 1_000_000) return `${trim(abs / 10_000, 1)}만`;
  if (abs < 100_000_000) return `${intFmt.format(Math.round(abs / 10_000))}만`;
  return `${trim(abs / 100_000_000, 2)}억`;
}

function trim(v: number, digits: number): string {
  return v.toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

/** KRW with sub-won precision (EPC, RPI): ₩27.0 · ₩0.216 */
export function krwPrecise(n: number | null | undefined, digits = 1): string {
  if (!finite(n)) return DASH;
  return `${n < 0 ? "-" : ""}₩${Math.abs(n).toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function int(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  return intFmt.format(Math.round(n));
}

export function compact(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  const v = Math.round(n);
  return `${v < 0 ? "-" : ""}${koUnits(Math.abs(v))}`;
}

/** Views: 1,234회 · 3.2만회 */
export function views(n: number | null | undefined): string {
  if (!finite(n)) return DASH;
  return `${compact(n)}회`;
}

/** 0.0123 → "1.23%" */
export function pct(fraction: number | null | undefined, digits = 1): string {
  if (!finite(fraction)) return DASH;
  return `${(fraction * 100).toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
}

export function pctSigned(fraction: number | null | undefined, digits = 0): string {
  if (!finite(fraction)) return DASH;
  const s = pct(fraction, digits);
  return fraction > 0 ? `+${s}` : s;
}

/** Ratio like median/I*: 1.24× */
export function ratio(n: number | null | undefined, digits = 2): string {
  if (!finite(n)) return DASH;
  return `${n.toFixed(digits)}×`;
}

export function seconds(s: number | null | undefined): string {
  if (!finite(s)) return DASH;
  if (s < 59.5) return `${s.toFixed(s < 10 ? 1 : 0)}초`;
  const total = Math.round(s);
  if (total >= 3600) {
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    return m ? `${h}시간 ${m}분` : `${h}시간`;
  }
  const m = Math.floor(total / 60);
  const r = total % 60;
  return r ? `${m}분 ${r}초` : `${m}분`;
}

function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

const dtFmt = new Intl.DateTimeFormat("ko-KR", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const dateFmt = new Intl.DateTimeFormat("ko-KR", { timeZone: TZ, month: "numeric", day: "numeric" });
const timeFmt = new Intl.DateTimeFormat("ko-KR", {
  timeZone: TZ,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function parts(d: Date) {
  const p = Object.fromEntries(dtFmt.formatToParts(d).map((x) => [x.type, x.value]));
  return p as Record<string, string>;
}

/** 2026-10-08 19:30 (KST) */
export function kst(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return DASH;
  const p = parts(d);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

/** 10. 8. */
export function kstDate(iso: string | null | undefined): string {
  const d = toDate(iso);
  return d ? dateFmt.format(d) : DASH;
}

/** 19:30 */
export function kstTime(iso: string | null | undefined): string {
  const d = toDate(iso);
  return d ? timeFmt.format(d) : DASH;
}

/** Day labels for `YYYY-MM-DD` series dates (already calendar dates): "10/8" */
export function dayLabel(date: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(date);
  if (!m) return date;
  return `${Number(m[2])}/${Number(m[3])}`;
}

/** "3분 전" / "2시간 후" relative to `now` (ms). */
export function relative(iso: string | null | undefined, now: number): string {
  const d = toDate(iso);
  if (!d || !now) return DASH;
  const diff = d.getTime() - now;
  const abs = Math.abs(diff);
  const suffix = diff < 0 ? "전" : "후";
  const min = Math.round(abs / 60_000);
  if (min < 1) return "방금";
  if (min < 60) return `${min}분 ${suffix}`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h}시간 ${suffix}`;
  return `${Math.round(h / 24)}일 ${suffix}`;
}

/** Duration between two ISO times: "4분 12초" */
export function elapsed(start: string | null | undefined, end: string | null | undefined, now: number): string {
  const s = toDate(start);
  if (!s) return DASH;
  const e = toDate(end) ?? (now ? new Date(now) : null);
  if (!e) return DASH;
  return seconds(Math.max(0, (e.getTime() - s.getTime()) / 1000));
}

/** [9, 23] → "09:00–23:00 KST" */
export function hourWindow(w: readonly [number, number] | null | undefined): string {
  if (!w) return DASH;
  const h = (x: number) => `${String(Math.max(0, Math.min(24, x))).padStart(2, "0")}:00`;
  return `${h(w[0])}–${h(w[1])} KST`;
}

export function shortId(id: string | number | null | undefined): string {
  if (id === null || id === undefined) return DASH;
  const s = String(id);
  return s.length > 10 ? s.slice(0, 8) : s;
}

/** Normalises a percentile given as 0–1 or 0–100 into "상위 0.8%". */
export function topPercent(percentile: number): string {
  const p = percentile <= 1 ? percentile * 100 : percentile;
  const top = Math.max(0, 100 - p);
  return `상위 ${top < 1 ? top.toFixed(1) : top.toFixed(0)}%`;
}

/* ------------------------------------------------------------------ KST calendar helpers */

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const KST_OFFSET = 9 * HOUR_MS;
export const WEEKDAYS_KO = ["일", "월", "화", "수", "목", "금", "토"];

/** UTC ms of 00:00 KST on the day containing `ms`. */
export function kstDayStart(ms: number): number {
  return Math.floor((ms + KST_OFFSET) / DAY_MS) * DAY_MS - KST_OFFSET;
}

/** UTC ms of Monday 00:00 KST of the week containing `ms`. */
export function kstWeekStart(ms: number): number {
  const day = kstDayStart(ms);
  const dow = new Date(day + KST_OFFSET).getUTCDay();
  return day - ((dow + 6) % 7) * DAY_MS;
}

/** "YYYY-MM-DD" of the KST calendar day. */
export function kstDayKey(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "";
  const ms = typeof value === "number" ? value : new Date(value).getTime();
  if (Number.isNaN(ms)) return "";
  return new Date(ms + KST_OFFSET).toISOString().slice(0, 10);
}

/** "10/8 (수)" for a KST day start. */
export function kstDayLabel(dayStartMs: number): string {
  const d = new Date(dayStartMs + KST_OFFSET);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()} (${WEEKDAYS_KO[d.getUTCDay()]})`;
}

export { DAY_MS };
