"use client";

import Image from "next/image";
import { useState, type ReactNode } from "react";
import { copyText } from "@/lib/hooks";
import { Button } from "./Button";
import { Icon, type IconName } from "./Icon";
import { useToast } from "./Toast";

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        {eyebrow ? <div className="mb-1 text-[13px] text-muted">{eyebrow}</div> : null}
        <h1 className="text-xl font-bold tracking-tight text-ink sm:text-2xl">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** Stat tile: label · value · optional sub line. Label is always explicit (never a bare number). */
export function StatTile({
  label,
  value,
  sub,
  tone,
  icon,
  className = "",
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "good" | "critical" | "warn";
  icon?: IconName;
  className?: string;
}) {
  const valueColor = tone === "good" ? "text-good-ink" : tone === "critical" ? "text-critical-ink" : "text-ink";
  return (
    <div className={`rounded-xl border border-line bg-surface p-4 shadow-card ${className}`}>
      <div className="flex items-center gap-1.5 text-[13px] text-muted">
        {icon ? <Icon name={icon} size={14} /> : null}
        {label}
      </div>
      <div className={`mt-1.5 text-xl font-semibold tracking-tight sm:text-2xl ${valueColor}`}>{value}</div>
      {sub ? <div className="mt-1 text-xs text-muted">{sub}</div> : null}
    </div>
  );
}

/**
 * The backend's Coupang *sandbox* returns placeholder CDN paths (/image/sandbox/…) that do not exist;
 * skip them instead of producing a 404 per product card.
 */
export function usableImageUrl(src: string | null | undefined): string | null {
  if (!src) return null;
  return /\/image\/sandbox\//.test(src) ? null : src;
}

/** Image with graceful fallback (signed R2 URLs expire; data: URIs in mock mode). */
export function Thumb({
  src: rawSrc,
  alt,
  className = "",
  ratio = "square",
  fallbackIcon = "film",
}: {
  src: string | null | undefined;
  alt: string;
  className?: string;
  ratio?: "square" | "portrait";
  fallbackIcon?: IconName;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const aspect = ratio === "portrait" ? "aspect-[9/16]" : "aspect-square";
  const src = usableImageUrl(rawSrc);
  if (!src || failed === src) {
    return (
      <div className={`flex items-center justify-center bg-surface-2 text-muted ${aspect} ${className}`} role="img" aria-label={`${alt} (이미지 없음)`}>
        <Icon name={fallbackIcon} size={22} />
      </div>
    );
  }
  return (
    <div className={`relative overflow-hidden bg-surface-2 ${aspect} ${className}`}>
      <Image src={src} alt={alt} fill unoptimized sizes="(max-width: 768px) 100vw, 320px" className="object-cover" onError={() => setFailed(src)} />
    </div>
  );
}

export function CopyButton({ text, label = "복사", size = "sm" }: { text: string; label?: string; size?: "sm" | "md" }) {
  const toast = useToast();
  const [done, setDone] = useState(false);
  return (
    <Button
      size={size}
      icon={done ? "check" : "copy"}
      onClick={async () => {
        const ok = await copyText(text);
        if (ok) {
          setDone(true);
          toast("클립보드에 복사했습니다");
          setTimeout(() => setDone(false), 1600);
        } else {
          toast("복사하지 못했습니다 — 직접 선택해 복사하세요", "critical");
        }
      }}
    >
      {done ? "복사됨" : label}
    </Button>
  );
}

/** Radio-group styled as a segmented control. */
export function Segmented<T extends string | number>({
  value,
  onChange,
  options,
  label,
  size = "md",
}: {
  value: T;
  onChange: (v: T) => void;
  options: Array<{ value: T; label: ReactNode; disabled?: boolean }>;
  label: string;
  size?: "sm" | "md";
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-lg border border-line bg-surface-2 p-0.5">
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={o.disabled}
            onClick={() => onChange(o.value)}
            className={`${size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3 text-sm"} shrink-0 whitespace-nowrap rounded-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              active ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** Horizontal progress meter (e.g. spent vs cap). Severity drives the fill. */
export function Meter({
  value,
  max,
  label,
  valueText,
  className = "",
}: {
  value: number;
  max: number | null | undefined;
  label: string;
  valueText: string;
  className?: string;
}) {
  const frac = max && max > 0 ? Math.min(1, value / max) : 0;
  const over = !!max && value > max;
  const fill = over ? "bg-critical" : frac >= 0.9 ? "bg-serious" : frac >= 0.7 ? "bg-warn" : "bg-accent";
  return (
    <div className={className}>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max ?? undefined}
        aria-valuenow={value}
        aria-valuetext={valueText}
        className="h-2 w-full overflow-hidden rounded-full bg-accent-soft"
      >
        <div className={`h-full rounded-full ${fill}`} style={{ width: `${Math.max(frac * 100, value > 0 ? 2 : 0)}%` }} />
      </div>
    </div>
  );
}
