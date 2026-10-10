"use client";

import { useId, type ReactNode } from "react";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { Pill } from "@/components/ui/Pill";
import type { ClaimShape, LintDuration, LintIssue } from "@/lib/schemas";
import { BEAT_ROLE, CTA_TAIL_SECONDS, HOOK_TYPE_LABEL, SYLLABLES_PER_SECOND, evidenceLabel, syllableCount } from "./shared";

export interface LocalBeat {
  role: string;
  start: number;
  end: number;
  narration: string;
  on_screen_text: string;
}

/** Text with the sentences flagged by lint underlined (wavy red = error, amber = warning). */
function Marked({ text, issues }: { text: string; issues: LintIssue[] }) {
  const hits = issues
    .map((i) => ({ i, at: i.sentence ? text.indexOf(i.sentence) : -1 }))
    .filter((h) => h.at >= 0)
    .sort((a, b) => a.at - b.at);
  if (!hits.length) return null;
  const out: ReactNode[] = [];
  let cursor = 0;
  hits.forEach(({ i, at }, k) => {
    if (at < cursor) return;
    if (at > cursor) out.push(<span key={`t${k}`}>{text.slice(cursor, at)}</span>);
    out.push(
      <span
        key={`m${k}`}
        title={i.message}
        className={`underline decoration-wavy decoration-2 underline-offset-4 ${i.severity === "error" ? "decoration-critical" : "decoration-warn"}`}
      >
        {i.sentence}
      </span>,
    );
    cursor = at + i.sentence.length;
  });
  if (cursor < text.length) out.push(<span key="tail">{text.slice(cursor)}</span>);
  return (
    <p className="mt-1 whitespace-pre-wrap rounded-md bg-surface-2 px-2.5 py-1.5 text-[13px] leading-relaxed text-ink-2" aria-label="검수 지적 문장 표시">
      {out}
    </p>
  );
}

function fieldIssues(text: string, issues: LintIssue[]): LintIssue[] {
  return issues.filter((i) => i.sentence && text.includes(i.sentence));
}

const AREA =
  "w-full rounded-lg border border-line-strong bg-surface px-3 py-2 text-sm leading-relaxed text-ink placeholder:text-muted focus:border-accent focus:outline-none disabled:opacity-60";

/**
 * Spoken length at the fast short-form rate (+ CTA hold). The server's estimate (which reads digits the way
 * the voice says them, e.g. "3,812" → 삼천팔백십이) wins once live lint answers; the local count is the fallback.
 */
export function DurationBar({
  narration,
  min,
  max,
  rate = SYLLABLES_PER_SECOND,
  server,
}: {
  narration: string;
  min: number;
  max: number;
  rate?: number;
  server?: LintDuration | null;
}) {
  const syll = server?.syllables ?? syllableCount(narration);
  const raw = server?.estimated_seconds ?? syll / rate + CTA_TAIL_SECONDS;
  const est = Math.min(max, Math.max(min, raw));
  const scaleMax = max + 5;
  const at = (v: number) => `${Math.min(100, (v / scaleMax) * 100)}%`;
  const state = raw > max ? "over" : raw < min ? "under" : "ok";
  return (
    <div>
      <div className="flex items-baseline justify-between text-[13px]">
        <span className="text-muted">예상 영상 길이</span>
        <span className={`font-semibold tabular-nums ${state === "over" ? "text-critical-ink" : "text-ink"}`}>
          {state === "over" ? raw.toFixed(1) : Math.round(est)}초{" "}
          <span className="font-normal text-muted">
            (음절 {syll} ÷ {rate}/초 + CTA {CTA_TAIL_SECONDS}초)
          </span>
        </span>
      </div>
      <div className="relative mt-1.5 h-2 rounded-full bg-surface-2" role="meter" aria-label="예상 영상 길이" aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(est)}>
        <div className="absolute inset-y-0 rounded-full bg-good-soft" style={{ left: at(min), width: `calc(${at(max)} - ${at(min)})` }} />
        <div className={`absolute inset-y-0 left-0 rounded-full ${state === "ok" ? "bg-accent" : "bg-serious"}`} style={{ width: at(Math.min(raw, scaleMax)) }} />
      </div>
      <p className={`mt-1 text-xs ${state === "over" ? "text-critical-ink" : "text-muted"}`}>
        빠른 보이스 기준 · 쇼츠는 {min}–{max}초 상품 설명
        {state === "over"
          ? ` — ${max}초를 넘으면 생성할 수 없어요. 문장을 줄여 주세요`
          : state === "under"
            ? " — 짧으면 마지막 CTA 화면으로 채워집니다"
            : ""}
      </p>
    </div>
  );
}

function IssueList({ issues, onApply, readOnly }: { issues: LintIssue[]; onApply: (i: LintIssue) => void; readOnly: boolean }) {
  if (!issues.length) {
    return (
      <p className="flex items-center gap-1.5 text-[13px] text-good-ink">
        <Icon name="check" size={14} /> 금칙어·근거 없는 주장·가격 불일치가 없습니다
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {issues.map((i, k) => (
        <li key={`${i.sentence}-${k}`} className={`rounded-lg border px-3 py-2 text-[13px] ${i.severity === "error" ? "border-critical/40 bg-critical-soft" : "border-warn/40 bg-warn-soft"}`}>
          <p className="flex flex-wrap items-center gap-1.5">
            <Pill tone={i.severity === "error" ? "critical" : "warn"} icon="alert">
              {i.severity === "error" ? "오류" : "주의"}
            </Pill>
            <span className="font-medium text-ink">{i.message}</span>
          </p>
          {i.sentence ? <p className="mt-1 text-ink-2">“{i.sentence}”</p> : null}
          {i.suggestion ? (
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted">
                제안: {i.suggestion === "문장 삭제" ? "문장 삭제" : `“${i.term}” → “${i.suggestion}”`}
              </span>
              {!readOnly ? (
                <button type="button" onClick={() => onApply(i)} className="rounded-md border border-line-strong bg-surface px-2 py-0.5 text-xs font-medium text-ink hover:border-accent">
                  제안 적용
                </button>
              ) : null}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ClaimList({ claims }: { claims: ClaimShape[] }) {
  if (!claims.length) return <p className="text-[13px] text-muted">근거가 필요한 사실 문장이 아직 없습니다.</p>;
  return (
    <ul className="space-y-1.5">
      {claims.map((c) => (
        <li key={`${c.id}-${c.text}`} className="flex items-start gap-2 text-[13px]">
          <Icon name={c.grounded ? "check" : "alert"} size={14} className={`mt-0.5 ${c.grounded ? "text-good-ink" : "text-critical-ink"}`} label={c.grounded ? "근거 있음" : "근거 없음"} />
          <span className="min-w-0 flex-1">
            <span className="text-ink">{c.text}</span>{" "}
            {c.evidence.length ? (
              Array.from(new Set(c.evidence.map(evidenceLabel))).map((e) => (
                <span key={e} className="ml-1 inline-flex rounded bg-accent-soft px-1.5 text-[11px] font-medium text-accent-ink">
                  {e}
                </span>
              ))
            ) : (
              <span className="ml-1 text-[11px] text-muted">근거 불필요</span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ScriptPanel({
  kind,
  hookType,
  hooks,
  chosen,
  beats,
  body,
  issues,
  claims,
  limitChars,
  minSeconds,
  maxSeconds,
  speechRate,
  duration,
  readOnly,
  linting,
  onHook,
  onChoose,
  onBeat,
  onBody,
  onApply,
}: {
  kind: "short" | "text";
  hookType: string | null | undefined;
  hooks: string[];
  chosen: number;
  beats: LocalBeat[];
  body: string;
  issues: LintIssue[];
  claims: ClaimShape[];
  limitChars: number;
  minSeconds: number;
  maxSeconds: number;
  /** Fast-delivery estimate rate (options.limits.syllables_per_second). */
  speechRate?: number;
  /** Server estimate from lint (shorts). */
  duration?: LintDuration | null;
  readOnly: boolean;
  linting: boolean;
  onHook: (i: number, v: string) => void;
  onChoose: (i: number) => void;
  onBeat: (i: number, field: "narration" | "on_screen_text", v: string) => void;
  onBody: (v: string) => void;
  onApply: (issue: LintIssue) => void;
}) {
  const uid = useId();
  const narration = beats.map((b) => b.narration).join(" ");
  const chars = kind === "short" ? Array.from(beats.map((b) => b.narration).join("")).length : Array.from(body).length;
  const charLimit = kind === "short" ? limitChars : 4000;
  const errors = issues.filter((i) => i.severity === "error").length;

  return (
    <Card aria-labelledby={`${uid}-title`}>
      <CardHeader
        id={`${uid}-title`}
        title={
          <span className="flex items-center gap-2">
            <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-accent-fill text-xs font-bold text-white">2</span>
            {kind === "short" ? "대본" : "본문"}
          </span>
        }
        description="AI가 채운 초안을 그대로 쓰거나 고치세요. 모든 사실 문장은 상품 팩트로 자동 검증됩니다."
        actions={
          <span className={`text-xs tabular-nums ${chars > charLimit ? "font-semibold text-critical-ink" : "text-muted"}`}>
            {chars} / {charLimit}자
          </span>
        }
      />
      <CardBody className="space-y-5">
        <fieldset>
          <legend className="mb-2 text-[13px] font-semibold text-ink-2">
            훅 3안 {hookType ? <span className="font-normal text-muted">· {HOOK_TYPE_LABEL[hookType] ?? hookType}형</span> : null}
          </legend>
          <div className="space-y-1.5">
            {hooks.map((h, i) => (
              <div key={i} className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 ${i === chosen ? "border-accent bg-accent-soft" : "border-line"}`}>
                <input
                  type="radio"
                  name={`${uid}-hook`}
                  aria-label={`훅 ${i + 1} 선택`}
                  checked={i === chosen}
                  onChange={() => onChoose(i)}
                  disabled={readOnly}
                  className="h-4 w-4 accent-[var(--accent)]"
                />
                <input
                  value={h}
                  onChange={(e) => onHook(i, e.target.value)}
                  disabled={readOnly}
                  maxLength={120}
                  aria-label={`훅 ${i + 1}`}
                  className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none disabled:opacity-60"
                />
              </div>
            ))}
          </div>
        </fieldset>

        {kind === "short" ? (
          <>
            <ol className="space-y-3">
              {beats.map((b, i) => {
                const nIssues = fieldIssues(b.narration, issues);
                const sIssues = fieldIssues(b.on_screen_text, issues);
                return (
                  <li key={i} className="rounded-xl border border-line p-3">
                    <div className="mb-2 flex items-center gap-2 text-xs">
                      <Pill tone={b.role === "hook" ? "info" : "neutral"}>{BEAT_ROLE[b.role] ?? b.role}</Pill>
                      <span className="tabular-nums text-muted">
                        {b.start}–{b.end}초
                      </span>
                      {i === 0 && b.role === "hook" ? <span className="text-muted">· 선택한 훅이 들어갑니다</span> : null}
                    </div>
                    <label htmlFor={`${uid}-n${i}`} className="sr-only">
                      {i + 1}번 비트 내레이션
                    </label>
                    <textarea
                      id={`${uid}-n${i}`}
                      rows={2}
                      value={b.narration}
                      onChange={(e) => onBeat(i, "narration", e.target.value)}
                      disabled={readOnly || (i === 0 && b.role === "hook")}
                      maxLength={400}
                      placeholder="내레이션"
                      className={AREA}
                    />
                    <Marked text={b.narration} issues={nIssues} />
                    <label htmlFor={`${uid}-s${i}`} className="sr-only">
                      {i + 1}번 비트 화면 자막
                    </label>
                    <input
                      id={`${uid}-s${i}`}
                      value={b.on_screen_text}
                      onChange={(e) => onBeat(i, "on_screen_text", e.target.value)}
                      disabled={readOnly}
                      maxLength={120}
                      placeholder="화면 자막 (짧게)"
                      className={`${AREA} mt-1.5 h-9 py-0`}
                    />
                    <Marked text={b.on_screen_text} issues={sIssues} />
                  </li>
                );
              })}
            </ol>
            <DurationBar narration={narration} min={minSeconds} max={maxSeconds} rate={speechRate} server={duration} />
          </>
        ) : (
          <div>
            <label htmlFor={`${uid}-body`} className="mb-1.5 block text-[13px] font-semibold text-ink-2">
              본문 (첫 줄 = 대가성 문구)
            </label>
            <textarea id={`${uid}-body`} rows={12} value={body} onChange={(e) => onBody(e.target.value)} disabled={readOnly} maxLength={4000} className={AREA} />
            <Marked text={body} issues={fieldIssues(body, issues)} />
          </div>
        )}

        <section aria-labelledby={`${uid}-lint`}>
          <h3 id={`${uid}-lint`} className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-ink-2">
            실시간 검수
            {linting ? <span className="text-xs font-normal text-muted">확인 중…</span> : null}
            {errors ? <Pill tone="critical">오류 {errors}</Pill> : null}
          </h3>
          <IssueList issues={issues} onApply={onApply} readOnly={readOnly} />
        </section>

        <section aria-labelledby={`${uid}-claims`}>
          <h3 id={`${uid}-claims`} className="mb-2 text-[13px] font-semibold text-ink-2">
            주장 · 근거
          </h3>
          <ClaimList claims={claims} />
        </section>
      </CardBody>
    </Card>
  );
}
