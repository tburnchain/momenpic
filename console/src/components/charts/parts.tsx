"use client";

import { useState, type ReactNode } from "react";

/** Legend: mirrors the mark (line key for lines, rounded swatch for bars/areas). */
export function Legend({
  items,
  kind = "line",
  className = "",
}: {
  items: Array<{ key: string; label: ReactNode; color: string; value?: ReactNode }>;
  kind?: "line" | "box";
  className?: string;
}) {
  return (
    <ul className={`flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px] text-ink-2 ${className}`}>
      {items.map((it) => (
        <li key={it.key} className="inline-flex items-center gap-1.5">
          {kind === "line" ? (
            <span className="h-[2px] w-3.5 rounded-full" style={{ background: it.color }} aria-hidden />
          ) : (
            <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: it.color }} aria-hidden />
          )}
          <span>{it.label}</span>
          {it.value !== undefined ? <span className="font-semibold tabular-nums text-ink">{it.value}</span> : null}
        </li>
      ))}
    </ul>
  );
}

/** Collapsible table view of a chart's data (accessibility + exact values). */
export function ChartTable({
  caption,
  columns,
  rows,
}: {
  caption: string;
  columns: string[];
  rows: Array<Array<ReactNode>>;
}) {
  return (
    <details className="group mt-3 text-sm">
      <summary className="inline-flex cursor-pointer items-center gap-1 rounded text-[13px] text-accent-ink hover:underline">
        표로 보기
      </summary>
      <div className="mt-2 max-h-72 overflow-auto rounded-lg border border-line">
        <table className="w-full text-left text-[13px]">
          <caption className="sr-only">{caption}</caption>
          <thead className="sticky top-0 bg-surface-2 text-muted">
            <tr>
              {columns.map((c, i) => (
                <th key={c} scope="col" className={`px-3 py-2 font-medium ${i > 0 ? "text-right" : ""}`}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-line">
                {r.map((cell, j) => (
                  <td key={j} className={`px-3 py-1.5 ${j > 0 ? "text-right tabular-nums" : ""}`}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export interface BarItem {
  key: string;
  label: ReactNode;
  value: number;
  color?: string;
  /** Text shown at the bar tip (defaults to the formatted value). */
  display?: ReactNode;
  hint?: string;
}

/** Horizontal bar list: ≤ 24 px bars from one baseline, value at the tip, label on the left. */
export function BarList({
  items,
  format = (v) => v.toLocaleString("ko-KR"),
  max,
  labelWidth = "7.5rem",
  ariaLabel,
}: {
  items: BarItem[];
  format?: (v: number) => string;
  max?: number;
  labelWidth?: string;
  ariaLabel: string;
}) {
  const m = max ?? Math.max(1, ...items.map((i) => i.value));
  return (
    <ul className="space-y-2" aria-label={ariaLabel}>
      {items.map((it) => {
        const frac = Math.max(0, Math.min(1, it.value / m));
        return (
          <li
            key={it.key}
            className="grid items-center gap-3 text-[13px]"
            style={{ gridTemplateColumns: `${labelWidth} minmax(0,1fr)` }}
            title={it.hint}
          >
            <span className="truncate text-ink-2">{it.label}</span>
            <span className="flex min-w-0 items-center gap-2">
              <span className="relative h-3 min-w-0 flex-1" aria-hidden>
                <span
                  className="absolute inset-y-0 left-0 rounded-r-[4px]"
                  style={{
                    width: `${frac * 100}%`,
                    minWidth: it.value > 0 ? 3 : 0,
                    background: it.color ?? "var(--s1)",
                  }}
                />
              </span>
              <span className="w-16 shrink-0 text-right font-medium tabular-nums text-ink">{it.display ?? format(it.value)}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Donut for ≤ 4 part-to-whole categories, 2 px surface gaps, total in the centre. */
export function Donut({
  items,
  size = 168,
  centerLabel,
  format = (v) => v.toLocaleString("ko-KR"),
}: {
  items: Array<{ key: string; label: string; value: number; color: string }>;
  size?: number;
  centerLabel: string;
  format?: (v: number) => string;
}) {
  const [active, setActive] = useState<string | null>(null);
  const total = items.reduce((a, b) => a + b.value, 0);
  const stroke = 22;
  const r = (size - stroke) / 2;
  const C = 2 * Math.PI * r;
  const gap = total > 0 && items.filter((i) => i.value > 0).length > 1 ? 2 : 0;
  const segments = items.reduce<Array<{ it: (typeof items)[number]; len: number; start: number }>>((acc, it) => {
    const prev = acc[acc.length - 1];
    acc.push({ it, len: total > 0 ? (it.value / total) * C : 0, start: prev ? prev.start + prev.len : 0 });
    return acc;
  }, []);
  const activeItem = items.find((i) => i.key === active);

  return (
    <div className="flex flex-wrap items-center gap-5">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${centerLabel} ${format(total)}: ${items.map((i) => `${i.label} ${format(i.value)}`).join(", ")}`}>
          <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
            <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-2)" strokeWidth={stroke} />
            {segments.map(({ it, len, start }) => {
              if (len <= 0) return null;
              const dash = Math.max(0, len - gap);
              return (
                <circle
                  key={it.key}
                  cx={size / 2}
                  cy={size / 2}
                  r={r}
                  fill="none"
                  stroke={it.color}
                  strokeWidth={active === it.key ? stroke + 4 : stroke}
                  strokeDasharray={`${dash} ${C - dash}`}
                  strokeDashoffset={-start}
                  onPointerEnter={() => setActive(it.key)}
                  onPointerLeave={() => setActive(null)}
                  style={{ transition: "stroke-width 120ms" }}
                />
              );
            })}
          </g>
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="text-[11px] text-muted">{activeItem ? activeItem.label : centerLabel}</span>
          <span className="text-lg font-semibold tabular-nums text-ink">{format(activeItem ? activeItem.value : total)}</span>
          {activeItem && total > 0 ? <span className="text-[11px] tabular-nums text-muted">{((activeItem.value / total) * 100).toFixed(1)}%</span> : null}
        </div>
      </div>
      <ul className="min-w-40 space-y-1.5 text-[13px]">
        {items.map((it) => (
          <li
            key={it.key}
            className="flex items-center gap-2"
            onPointerEnter={() => setActive(it.key)}
            onPointerLeave={() => setActive(null)}
          >
            <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: it.color }} aria-hidden />
            <span className="flex-1 text-ink-2">{it.label}</span>
            <span className="font-semibold tabular-nums text-ink">{format(it.value)}</span>
            <span className="w-12 text-right tabular-nums text-muted">{total > 0 ? `${((it.value / total) * 100).toFixed(1)}%` : "—"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Confidence-interval bar on a shared domain: thin track, CI band, mean dot. */
export function RangeBar({
  lo,
  hi,
  mean,
  domain,
  color = "var(--s1)",
  label,
}: {
  lo: number;
  hi: number;
  mean: number;
  domain: [number, number];
  color?: string;
  label: string;
}) {
  const span = domain[1] - domain[0] || 1;
  const p = (v: number) => `${Math.max(0, Math.min(100, ((v - domain[0]) / span) * 100))}%`;
  return (
    <div className="relative h-4 w-full min-w-28" role="img" aria-label={label}>
      <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-[var(--grid)]" />
      <div
        className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full"
        style={{ left: p(lo), width: `calc(${p(hi)} - ${p(lo)})`, background: color, opacity: 0.35 }}
      />
      <div
        className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[var(--surface)]"
        style={{ left: p(mean), background: color }}
      />
    </div>
  );
}

/** KPI #1 bar: channel median views ÷ I*, with the break-even (1.0×) marker. */
export function RatioBar({ ratio, max = 2 }: { ratio: number | null | undefined; max?: number }) {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return <span className="text-muted">—</span>;
  const m = Math.max(max, ratio);
  const pct = (v: number) => `${Math.min(100, (v / m) * 100)}%`;
  const ok = ratio >= 1;
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-2.5 min-w-24 flex-1 rounded-full bg-surface-2" role="img" aria-label={`중앙값/I* ${ratio.toFixed(2)}배, 손익분기 ${ok ? "초과" : "미달"}`}>
        <div className={`absolute inset-y-0 left-0 rounded-full ${ok ? "bg-good" : "bg-serious"}`} style={{ width: pct(ratio) }} />
        <div className="absolute -inset-y-1 w-px bg-ink" style={{ left: pct(1) }} title="손익분기 1.0×" />
      </div>
      <span className="w-12 text-right text-[13px] font-semibold tabular-nums text-ink">{ratio.toFixed(2)}×</span>
    </div>
  );
}
