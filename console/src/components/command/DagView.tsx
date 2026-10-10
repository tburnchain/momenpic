"use client";

import { useMemo, useState } from "react";
import { MODULE_LABEL } from "@/lib/labels";
import type { PlanStep } from "@/lib/schemas";

const NODE_W = 172;
const NODE_H = 30;
const ROW_GAP = 8;
const COL_GAP = 52;
const HEAD = 26;

interface Placed {
  step: PlanStep;
  layer: number;
  y: number;
}

/**
 * Layered DAG layout: layer = longest path from a root; within a layer nodes are placed at the
 * mean height of their parents (then pushed down to avoid overlap), followed by one upward pass
 * that centres parents over their children. Good enough for plan DAGs of a few dozen steps.
 */
function layout(steps: PlanStep[]) {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const layerOf = new Map<string, number>();
  const visiting = new Set<string>();
  const depth = (id: string): number => {
    const cached = layerOf.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0; // cycle guard — a plan should never have one
    visiting.add(id);
    const s = byId.get(id);
    const deps = (s?.depends_on ?? []).filter((d) => byId.has(d));
    const l = deps.length ? 1 + Math.max(...deps.map(depth)) : 0;
    visiting.delete(id);
    layerOf.set(id, l);
    return l;
  };
  steps.forEach((s) => depth(s.id));

  const layers: PlanStep[][] = [];
  steps.forEach((s) => {
    const l = layerOf.get(s.id) ?? 0;
    (layers[l] ??= []).push(s);
  });

  const y = new Map<string, number>();
  const children = new Map<string, string[]>();
  steps.forEach((s) => s.depends_on.forEach((d) => byId.has(d) && children.set(d, [...(children.get(d) ?? []), s.id])));

  const pack = (nodes: PlanStep[], desired: (s: PlanStep) => number) => {
    const order = nodes.map((s, i) => ({ s, d: desired(s), i })).sort((a, b) => a.d - b.d || a.i - b.i);
    let cursor = -Infinity;
    for (const { s, d } of order) {
      const pos = Math.max(d, cursor);
      y.set(s.id, pos);
      cursor = pos + NODE_H + ROW_GAP;
    }
  };

  layers.forEach((nodes, l) => {
    if (l === 0) {
      nodes.forEach((s, i) => y.set(s.id, i * (NODE_H + ROW_GAP)));
      return;
    }
    pack(nodes, (s) => {
      const ps = s.depends_on.map((d) => y.get(d)).filter((v): v is number => v !== undefined);
      return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : 0;
    });
  });

  // Upward pass: centre parents on their children.
  for (let l = layers.length - 2; l >= 0; l--) {
    pack(layers[l], (s) => {
      const cs = (children.get(s.id) ?? []).map((c) => y.get(c)).filter((v): v is number => v !== undefined);
      return cs.length ? cs.reduce((a, b) => a + b, 0) / cs.length : (y.get(s.id) ?? 0);
    });
  }

  const minY = Math.min(0, ...Array.from(y.values()));
  const placed: Placed[] = steps.map((s) => ({ step: s, layer: layerOf.get(s.id) ?? 0, y: (y.get(s.id) ?? 0) - minY }));
  const height = Math.max(...placed.map((p) => p.y + NODE_H), NODE_H);
  return { placed, layers, height };
}

function shortParams(p: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(p)) {
    if (v === null || v === undefined) continue;
    parts.push(`${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  }
  return parts.join(", ");
}

export function DagView({ steps }: { steps: PlanStep[] }) {
  const { placed, layers, height } = useMemo(() => layout(steps), [steps]);
  const [focus, setFocus] = useState<string | null>(null);
  const pos = useMemo(() => new Map(placed.map((p) => [p.step.id, p])), [placed]);

  if (!steps.length) return <p className="text-sm text-muted">계획에 단계가 없습니다.</p>;

  const width = layers.length * NODE_W + (layers.length - 1) * COL_GAP;
  const totalH = height + HEAD + 4;
  const xOf = (layer: number) => layer * (NODE_W + COL_GAP);
  const related = new Set<string>();
  if (focus) {
    related.add(focus);
    pos.get(focus)?.step.depends_on.forEach((d) => related.add(d));
    placed.forEach((p) => p.step.depends_on.includes(focus) && related.add(p.step.id));
  }
  const focused = focus ? pos.get(focus)?.step : undefined;

  return (
    <div>
      <div className="max-h-[480px] overflow-auto rounded-lg border border-line bg-surface-2/60 p-3">
        <svg
          width={width}
          height={totalH}
          viewBox={`0 0 ${width} ${totalH}`}
          role="group"
          aria-label={`실행 DAG: ${steps.length}개 단계, ${layers.length}개 계층`}
          className="block"
        >
          {layers.map((nodes, l) => {
            const mods = Array.from(new Set(nodes.map((n) => n.module)));
            return (
              <text key={l} x={xOf(l)} y={14} fontSize={11} fill="var(--muted)" fontWeight={600}>
                {`${l + 1}단계 · ${mods.join("·")}`}
              </text>
            );
          })}
          <g transform={`translate(0 ${HEAD})`}>
            {placed.flatMap((p) =>
              p.step.depends_on.map((d) => {
                const from = pos.get(d);
                if (!from) return null;
                const x1 = xOf(from.layer) + NODE_W;
                const y1 = from.y + NODE_H / 2;
                const x2 = xOf(p.layer);
                const y2 = p.y + NODE_H / 2;
                const mx = (x1 + x2) / 2;
                const hot = focus !== null && (d === focus || p.step.id === focus);
                return (
                  <path
                    key={`${d}->${p.step.id}`}
                    d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                    fill="none"
                    stroke={hot ? "var(--accent)" : "var(--line-strong)"}
                    strokeWidth={hot ? 1.75 : 1}
                    opacity={focus && !hot ? 0.35 : 1}
                  />
                );
              }),
            )}
            {placed.map((p) => {
              const gate = p.step.module === "M7";
              const dim = focus !== null && !related.has(p.step.id);
              const product = typeof p.step.params.product === "number" ? ` #${p.step.params.product}` : "";
              const platform = typeof p.step.params.platform === "string" ? ` · ${p.step.params.platform}` : "";
              return (
                <g
                  key={p.step.id}
                  transform={`translate(${xOf(p.layer)} ${p.y})`}
                  tabIndex={0}
                  role="button"
                  aria-label={`${p.step.module} ${MODULE_LABEL[p.step.module] ?? ""} ${p.step.action}${product}${platform}. 선행 단계: ${p.step.depends_on.join(", ") || "없음"}`}
                  aria-pressed={focus === p.step.id}
                  onPointerEnter={() => setFocus(p.step.id)}
                  onPointerLeave={() => setFocus(null)}
                  onFocus={() => setFocus(p.step.id)}
                  onBlur={() => setFocus(null)}
                  className="cursor-default outline-none"
                  opacity={dim ? 0.4 : 1}
                >
                  <rect
                    width={NODE_W}
                    height={NODE_H}
                    rx={7}
                    fill={gate ? "var(--accent-soft)" : "var(--surface)"}
                    stroke={focus === p.step.id ? "var(--accent)" : gate ? "var(--accent)" : "var(--line-strong)"}
                    strokeWidth={focus === p.step.id || gate ? 1.5 : 1}
                  />
                  <text x={9} y={NODE_H / 2} dy="0.34em" fontSize={11.5} fill="var(--ink)">
                    <tspan fontWeight={700} fill={gate ? "var(--accent-ink)" : "var(--ink-2)"}>
                      {p.step.module}
                    </tspan>
                    <tspan dx={5}>{`${p.step.action}${product}${platform}`.slice(0, 24)}</tspan>
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
      </div>
      <p className="mt-2 min-h-5 text-xs text-muted" aria-live="polite">
        {focused ? (
          <>
            <span className="font-semibold text-ink">
              {focused.module} {MODULE_LABEL[focused.module] ?? ""} · {focused.action}
            </span>{" "}
            <span className="font-mono">{shortParams(focused.params) || "파라미터 없음"}</span>
          </>
        ) : (
          "노드에 마우스를 올리거나 Tab으로 이동하면 파라미터와 연결이 강조됩니다. 파란 테두리 = M7 단일 컴플라이언스 게이트."
        )}
      </p>
    </div>
  );
}
