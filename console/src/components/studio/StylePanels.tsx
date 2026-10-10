"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, Notice } from "@/components/ui/Feedback";
import { Icon, Spinner } from "@/components/ui/Icon";
import { Segmented } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import type { StudioOptions, StudioVoice } from "@/lib/schemas";
import { POSITION_LABEL, blobUrl, useBlobUrl } from "./shared";

function StepTitle({ n, children }: { n: number; children: ReactNode }) {
  return (
    <span className="flex items-center gap-2">
      <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-accent-fill text-xs font-bold text-white">{n}</span>
      {children}
    </span>
  );
}

const GENDER: Record<string, string> = { F: "여성", M: "남성", female: "여성", male: "남성" };
const AGE: Record<string, string> = { "20s": "20대", "30s": "30대", "40s": "40대", "50s": "50대" };

function isRecommended(v: StudioVoice, persona: string | null | undefined, hint: string | null | undefined): boolean {
  if (hint && hint === v.id) return true;
  if (!persona) return false;
  const tags = v.persona_tags.filter((t) => persona.includes(t));
  return tags.length >= 2;
}

/* ------------------------------------------------------------------ ③ watermark */

export function WatermarkCard({ value, onChange, max, placeholder, readOnly }: { value: string; onChange: (v: string) => void; max: number; placeholder: string; readOnly: boolean }) {
  return (
    <Card aria-labelledby="wm-title">
      <CardHeader id="wm-title" title={<StepTitle n={3}>워터마크</StepTitle>} description="채널명·브랜드명 (영상 구석에 작게 표시)" />
      <CardBody>
        <div className="flex items-center gap-2">
          <label htmlFor="wm-input" className="sr-only">
            워터마크
          </label>
          <input
            id="wm-input"
            value={value}
            onChange={(e) => onChange(e.target.value.slice(0, max))}
            maxLength={max}
            disabled={readOnly}
            placeholder={placeholder}
            className="h-10 min-w-0 flex-1 rounded-lg border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none disabled:opacity-60"
          />
          <span className="w-12 text-right text-xs tabular-nums text-muted">
            {Array.from(value).length}/{max}
          </span>
        </div>
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ ④ voice */

export function VoicePanel({
  options,
  persona,
  value,
  onChange,
  readOnly,
}: {
  options: StudioOptions;
  persona: string | null | undefined;
  value: string | null | undefined;
  onChange: (id: string) => void;
  readOnly: boolean;
}) {
  const toast = useToast();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const hint = options.personas.find((p) => p.name === persona)?.voice_hint;
  const tts = options.tts;
  const showNote = !!tts.note && (tts.real_voice === false || (tts.provider ?? "") !== "elevenlabs");

  useEffect(() => () => audio.current?.pause(), []);

  const play = async (id: string) => {
    if (playing === id) {
      audio.current?.pause();
      setPlaying(null);
      return;
    }
    audio.current?.pause();
    setLoading(id);
    try {
      const url = await blobUrl(`/studio/voices/${encodeURIComponent(id)}/preview`);
      audio.current ??= new Audio();
      const a = audio.current;
      a.src = url;
      a.onended = () => setPlaying(null);
      await a.play();
      setPlaying(id);
    } catch {
      setPlaying(null);
      toast("미리듣기를 재생하지 못했습니다", "critical");
    } finally {
      setLoading(null);
    }
  };

  return (
    <Card aria-labelledby="voice-title">
      <CardHeader
        id="voice-title"
        title={<StepTitle n={4}>보이스</StepTitle>}
        description="20~25초 상품 설명용 빠르고 간결한 보이스만 제공 · 페르소나에 맞게 자동 선택"
      />
      <CardBody className="space-y-3">
        {showNote ? <Notice tone="warn">{tts.note}</Notice> : null}
        {options.voices.length ? (
          <div role="radiogroup" aria-label="보이스" className="grid grid-cols-2 gap-2">
            {options.voices.map((v) => {
              const selected = v.id === value;
              const rec = isRecommended(v, persona, hint);
              return (
                <div
                  key={v.id}
                  className={`relative rounded-xl border p-2.5 transition-colors ${selected ? "border-accent bg-accent-soft" : "border-line hover:border-line-strong"}`}
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    disabled={readOnly}
                    onClick={() => onChange(v.id)}
                    className="flex w-full items-start gap-2 pr-7 text-left disabled:cursor-default"
                  >
                    <span
                      className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-bold ${
                        v.gender === "M" ? "bg-[color-mix(in_oklab,var(--s1)_22%,transparent)] text-ink" : "bg-[color-mix(in_oklab,var(--s5)_28%,transparent)] text-ink"
                      }`}
                      aria-hidden
                    >
                      {v.name.slice(0, 1)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1 text-sm font-semibold text-ink">
                        {v.name}
                        {rec ? <Pill tone="good">추천</Pill> : null}
                      </span>
                      <span className="block truncate text-[11px] text-muted">
                        {[GENDER[v.gender ?? ""] ?? v.gender, AGE[v.age_band ?? ""] ?? v.age_band, v.tone, v.rate ? `×${v.rate.toFixed(2)}` : null]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </span>
                  </button>
                  {v.description ? <p className="mt-1.5 line-clamp-2 text-[11px] leading-snug text-ink-2">{v.description}</p> : null}
                  <button
                    type="button"
                    onClick={() => void play(v.id)}
                    aria-label={`${v.name} 미리듣기 ${playing === v.id ? "정지" : "재생"}`}
                    className="absolute right-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded-full border border-line bg-surface text-ink-2 hover:text-ink"
                  >
                    {loading === v.id ? <Spinner size={13} /> : <Icon name={playing === v.id ? "pause" : "play"} size={12} />}
                  </button>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState icon="user" title="보이스 목록을 준비 중입니다" className="py-6">
            렌더 엔진이 기본 보이스로 생성합니다.
          </EmptyState>
        )}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ ⑤ captions */

function StyleTile({ id, label, description, selected, onSelect, disabled }: { id: string; label: string; description?: string | null; selected: boolean; onSelect: () => void; disabled: boolean }) {
  const { url, failed } = useBlobUrl(`/studio/caption-styles/${encodeURIComponent(id)}/preview.png`);
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      disabled={disabled}
      title={description ?? undefined}
      className={`w-32 shrink-0 snap-start overflow-hidden rounded-xl border-2 text-left transition-colors disabled:opacity-50 ${selected ? "border-accent" : "border-line hover:border-line-strong"}`}
    >
      <div className="flex aspect-[16/10] items-center justify-center bg-surface-3">
        {/* eslint-disable-next-line @next/next/no-img-element -- object URL from an authenticated blob */}
        {url ? <img src={url} alt={`${label} 자막 스타일 예시`} className="h-full w-full object-cover" /> : failed ? <span className="text-xs text-muted">{label}</span> : <Spinner size={14} />}
      </div>
      <p className="px-2 py-1 text-xs font-medium text-ink">{label}</p>
    </button>
  );
}

export function CaptionPanel({
  options,
  style,
  position,
  enabled,
  onStyle,
  onPosition,
  onEnabled,
  readOnly,
}: {
  options: StudioOptions;
  style: string | null | undefined;
  position: string | null | undefined;
  enabled: boolean;
  onStyle: (id: string) => void;
  onPosition: (p: string) => void;
  onEnabled: (v: boolean) => void;
  readOnly: boolean;
}) {
  const positions = (options.caption_positions.length ? options.caption_positions : ["upper", "middle", "bottom"]).slice().sort(
    (a, b) => ["upper", "middle", "bottom"].indexOf(a) - ["upper", "middle", "bottom"].indexOf(b),
  );
  return (
    <Card aria-labelledby="cap-title">
      <CardHeader id="cap-title" title={<StepTitle n={5}>자막 스타일</StepTitle>} description="단어 단위 자동 정렬 자막" />
      <CardBody className="space-y-4">
        {options.caption_styles.length ? (
          <div role="radiogroup" aria-label="자막 스타일" className="-mx-1 flex snap-x gap-2 overflow-x-auto px-1 pb-1">
            {options.caption_styles.map((s) => (
              <StyleTile key={s.id} id={s.id} label={s.label} description={s.description} selected={s.id === (style ?? "classic")} onSelect={() => onStyle(s.id)} disabled={readOnly || !enabled} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted">자막 스타일을 불러오는 중입니다 — 기본 스타일로 생성됩니다.</p>
        )}
        <label className="flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={!enabled} onChange={(e) => onEnabled(!e.target.checked)} disabled={readOnly} className="h-4 w-4 accent-[var(--accent)]" />
          자막 없이 생성
        </label>
        <div>
          <p className="mb-1.5 text-[13px] font-medium text-ink-2">자막 위치</p>
          <Segmented
            label="자막 위치"
            size="sm"
            value={position ?? "bottom"}
            onChange={(p) => !readOnly && enabled && onPosition(p)}
            options={positions.map((p) => ({ value: p, label: POSITION_LABEL[p] ?? p }))}
          />
        </div>
        <div>
          <p className="mb-1.5 text-[13px] font-medium text-ink-2">광고 표기</p>
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface-2 px-3 py-1 text-xs font-medium text-ink"
            title={options.disclosure.note ?? undefined}
          >
            <Icon name="shield" size={13} className="text-good-ink" />
            항상 표시 · 법적 의무
          </span>
          <p className="mt-1.5 text-xs text-muted">
            첫 {options.disclosure.seconds ?? 3}초 상단: “{options.disclosure.text}”. {options.disclosure.note}
          </p>
        </div>
      </CardBody>
    </Card>
  );
}
