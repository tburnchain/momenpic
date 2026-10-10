"use client";

import type { ReactNode } from "react";
import { describeError, isApiError } from "@/lib/errors";
import { API_BASE, MOCK_MODE } from "@/lib/config";
import { Button } from "./Button";
import { Icon, type IconName } from "./Icon";

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton rounded-md ${className}`} aria-hidden />;
}

/** Generic loading placeholder with an accessible status label. */
export function LoadingBlock({ rows = 4, label = "불러오는 중…", className = "" }: { rows?: number; label?: string; className?: string }) {
  return (
    <div role="status" aria-live="polite" className={`space-y-2.5 ${className}`}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={`h-4 ${i % 3 === 2 ? "w-2/3" : i % 3 === 1 ? "w-5/6" : "w-full"}`} />
      ))}
    </div>
  );
}

export function ErrorBanner({
  error,
  onRetry,
  retrying,
  compact,
  className = "",
}: {
  error: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  compact?: boolean;
  className?: string;
}) {
  const { title, detail } = describeError(error);
  const showBase = isApiError(error) && error.kind === "network" && !MOCK_MODE;
  return (
    <div
      role="alert"
      className={`flex flex-wrap items-start gap-3 rounded-xl border border-critical/30 bg-critical-soft px-4 ${compact ? "py-2.5" : "py-3.5"} text-sm ${className}`}
    >
      <Icon name="alert" size={18} className="mt-0.5 text-critical-ink" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-ink">{title}</p>
        {detail ? <p className="mt-0.5 text-ink-2">{detail}</p> : null}
        {showBase ? (
          <p className="mt-0.5 font-mono text-xs text-muted">
            {API_BASE}
            {isApiError(error) && error.path ? error.path : ""}
          </p>
        ) : null}
      </div>
      {onRetry ? (
        <Button size="sm" icon="refresh" onClick={onRetry} loading={retrying}>
          다시 시도
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  children,
  action,
  className = "",
}: {
  icon?: IconName;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col items-center justify-center px-6 py-12 text-center ${className}`}>
      <span className="mb-3 inline-flex h-11 w-11 items-center justify-center rounded-full bg-surface-2 text-muted">
        <Icon name={icon} size={20} />
      </span>
      <p className="font-semibold text-ink">{title}</p>
      {children ? <div className="mt-1 max-w-md text-sm text-muted">{children}</div> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
  className = "",
}: {
  tone?: "info" | "warn" | "good";
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  const style =
    tone === "warn"
      ? "border-warn/40 bg-warn-soft"
      : tone === "good"
        ? "border-good/30 bg-good-soft"
        : "border-accent/25 bg-accent-soft";
  const icon: IconName = tone === "warn" ? "alert" : tone === "good" ? "check" : "info";
  const iconColor = tone === "warn" ? "text-warn-ink" : tone === "good" ? "text-good-ink" : "text-accent-ink";
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 text-sm ${style} ${className}`}>
      <Icon name={icon} size={16} className={`mt-0.5 ${iconColor}`} />
      <div className="min-w-0 text-ink-2">
        {title ? <p className="font-medium text-ink">{title}</p> : null}
        {children}
      </div>
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-line-strong bg-surface-2 px-1 font-mono text-[11px] font-medium text-ink-2">
      {children}
    </kbd>
  );
}
