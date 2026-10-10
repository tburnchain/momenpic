"use client";

import { useId, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useElementWidth } from "@/lib/hooks";
import { linear, niceTicks, textWidth } from "./scale";

export interface LineSeries {
  key: string;
  label: string;
  /** CSS colour, normally a categorical slot var(--sN). */
  color: string;
  values: Array<number | null>;
  /** 10 % wash under the line (single-series magnitude charts). */
  area?: boolean;
}

export interface LineChartProps {
  x: string[];
  series: LineSeries[];
  height?: number;
  yFormat: (v: number) => string;
  yTickFormat?: (v: number) => string;
  /** Tooltip heading for index i (defaults to x[i]). */
  xTitle?: (i: number) => string;
  ariaLabel: string;
  refLines?: Array<{ y: number; label: string }>;
  vMarkers?: Array<{ index: number; label: string }>;
  yDomain?: [number, number];
  dimmed?: boolean;
  /** Force an x tick every N points (default: derived from the width). */
  xTickEvery?: number;
  /** Minimum y span when all values are equal (e.g. an all-zero money series). */
  minSpan?: number;
}

/**
 * Multi-series line chart: 2px lines, hairline grid, one y-axis, crosshair + tooltip that lists
 * every series at the hovered X (pointer or ←/→ keys). Legend lives outside (see <Legend>).
 */
export function LineChart({
  x,
  series,
  height = 240,
  yFormat,
  yTickFormat = yFormat,
  xTitle,
  ariaLabel,
  refLines = [],
  vMarkers = [],
  yDomain,
  dimmed,
  xTickEvery,
  minSpan = 1,
}: LineChartProps) {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const clipId = `clip${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const n = x.length;

  const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null && Number.isFinite(v)));
  for (const r of refLines) all.push(r.y);
  const dataMin = yDomain ? yDomain[0] : Math.min(0, ...all);
  const rawMax = yDomain ? yDomain[1] : Math.max(0, ...all);
  const dataMax = !yDomain && rawMax - dataMin < minSpan ? dataMin + minSpan : rawMax;
  const ticks = yDomain ? niceTicks(yDomain[0], yDomain[1], 4).filter((t) => t >= yDomain[0] && t <= yDomain[1]) : niceTicks(dataMin, dataMax, 4);
  const y0 = yDomain ? yDomain[0] : ticks[0];
  const y1 = yDomain ? yDomain[1] : ticks[ticks.length - 1];

  const left = Math.ceil(Math.max(...ticks.map((t) => textWidth(yTickFormat(t))), 20)) + 10;
  const right = 14;
  const top = 10;
  const bottom = 26;
  const plotW = Math.max(10, width - left - right);
  const plotH = height - top - bottom;

  const sx = (i: number) => left + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const sy = linear([y0, y1], [top + plotH, top]);

  const path = (vals: Array<number | null>) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (v === null || !Number.isFinite(v)) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const baseY = sy(Math.max(y0, Math.min(0, y1)));

  const autoEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 64))));
  const tickEvery = xTickEvery ? xTickEvery * Math.max(1, Math.ceil(autoEvery / xTickEvery)) : autoEvery;
  const xTicks = x.map((label, i) => ({ label, i })).filter(({ i }) => i % tickEvery === 0 || i === n - 1);
  if (xTicks.length >= 2) {
    const last = xTicks[xTicks.length - 1];
    const prev = xTicks[xTicks.length - 2];
    if (last.i - prev.i < tickEvery * 0.6) xTicks.splice(xTicks.length - 2, 1);
  }

  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const i = n <= 1 ? 0 : Math.round((px / rect.width) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!n) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      setHover((h) => {
        const cur = h ?? (e.key === "ArrowRight" ? -1 : n);
        return Math.max(0, Math.min(n - 1, cur + (e.key === "ArrowRight" ? 1 : -1)));
      });
    } else if (e.key === "Home") setHover(0);
    else if (e.key === "End") setHover(n - 1);
    else if (e.key === "Escape") setHover(null);
  };

  const tipLeft = hover !== null ? sx(hover) : 0;
  const tipOnLeft = hover !== null && tipLeft > width * 0.6;

  return (
    <div
      ref={ref}
      className={`relative w-full select-none rounded-md transition-opacity ${dimmed ? "opacity-60" : ""}`}
      style={{ height }}
      tabIndex={0}
      role="group"
      aria-label={`${ariaLabel}. 좌우 화살표로 값을 탐색할 수 있습니다.`}
      onKeyDown={onKey}
      onBlur={() => setHover(null)}
    >
      {width > 0 && n > 0 ? (
        <svg width={width} height={height} className="block overflow-visible" aria-hidden>
          <defs>
            <clipPath id={clipId}>
              <rect x={left} y={top - 6} width={plotW} height={plotH + 12} />
            </clipPath>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={left} x2={left + plotW} y1={sy(t)} y2={sy(t)} stroke={t === 0 ? "var(--baseline)" : "var(--grid)"} strokeWidth={1} shapeRendering="crispEdges" />
              <text x={left - 8} y={sy(t)} dy="0.32em" textAnchor="end" fontSize={11} fill="var(--axis)" className="tabular-nums">
                {yTickFormat(t)}
              </text>
            </g>
          ))}
          {xTicks.map(({ label, i }) => (
            <text key={i} x={sx(i)} y={height - 8} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} fontSize={11} fill="var(--axis)">
              {label}
            </text>
          ))}
          {refLines.map((r) => (
            <g key={r.label}>
              <line x1={left} x2={left + plotW} y1={sy(r.y)} y2={sy(r.y)} stroke="var(--ink-2)" strokeWidth={1} strokeDasharray="4 4" />
              <text x={left + plotW} y={sy(r.y) - 5} textAnchor="end" fontSize={11} fill="var(--ink-2)">
                {r.label}
              </text>
            </g>
          ))}
          {vMarkers.map((m) => (
            <g key={m.label}>
              <line x1={sx(m.index)} x2={sx(m.index)} y1={top} y2={top + plotH} stroke="var(--ink-2)" strokeWidth={1} strokeDasharray="4 4" />
              <text x={sx(m.index) + (sx(m.index) > left + plotW * 0.8 ? -5 : 5)} y={top + plotH - 6} textAnchor={sx(m.index) > left + plotW * 0.8 ? "end" : "start"} fontSize={11} fill="var(--ink-2)">
                {m.label}
              </text>
            </g>
          ))}
          <g clipPath={`url(#${clipId})`}>
            {series.map((s) =>
              s.area ? (
                <path
                  key={`${s.key}-area`}
                  d={`${path(s.values)}L${sx(n - 1).toFixed(1)},${baseY}L${sx(0).toFixed(1)},${baseY}Z`}
                  fill={s.color}
                  fillOpacity={0.1}
                />
              ) : null,
            )}
            {series.map((s) => (
              <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            ))}
          </g>
          {/* end markers */}
          {hover === null &&
            series.map((s) => {
              const i = s.values.length - 1;
              const v = s.values[i];
              if (v === null || v === undefined) return null;
              return <circle key={s.key} cx={sx(i)} cy={sy(v)} r={4} fill={s.color} stroke="var(--surface)" strokeWidth={2} />;
            })}
          {hover !== null ? (
            <g>
              <line x1={sx(hover)} x2={sx(hover)} y1={top} y2={top + plotH} stroke="var(--ink-2)" strokeOpacity={0.5} strokeWidth={1} />
              {series.map((s) => {
                const v = s.values[hover];
                if (v === null || v === undefined) return null;
                return <circle key={s.key} cx={sx(hover)} cy={sy(v)} r={4} fill={s.color} stroke="var(--surface)" strokeWidth={2} />;
              })}
            </g>
          ) : null}
          <rect
            x={left}
            y={top}
            width={plotW}
            height={plotH}
            fill="transparent"
            onPointerMove={onMove}
            onPointerDown={onMove}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
      ) : null}
      {hover !== null && width > 0 ? (
        <div
          className="pointer-events-none absolute z-10 min-w-36 rounded-lg border border-line bg-surface px-3 py-2 text-xs shadow-lg"
          style={{
            top: 4,
            left: tipOnLeft ? undefined : Math.min(tipLeft + 12, width - 150),
            right: tipOnLeft ? Math.max(4, width - tipLeft + 12) : undefined,
          }}
        >
          <p className="mb-1 font-medium text-muted">{xTitle ? xTitle(hover) : x[hover]}</p>
          <ul className="space-y-0.5">
            {series.map((s) => (
              <li key={s.key} className="flex items-center gap-2">
                <span className="h-0.5 w-3 rounded-full" style={{ background: s.color }} aria-hidden />
                <span className="font-semibold tabular-nums text-ink">{s.values[hover] === null ? "—" : yFormat(s.values[hover] as number)}</span>
                <span className="text-muted">{s.label}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
