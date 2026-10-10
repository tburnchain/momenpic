import type { ReactNode } from "react";
import {
  ACCOUNT_STATUS,
  PLATFORM_LABEL,
  ROLE_LABEL,
  RUN_STATUS,
  VERDICT,
  platformColor,
  publicationState,
  type Tone,
} from "@/lib/labels";
import type { AccountStatus, Platform, Role, RunStatus, Verdict } from "@/lib/schemas";
import { Icon, type IconName } from "./Icon";

const TONE: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-2 border-line",
  info: "bg-accent-soft text-accent-ink border-transparent",
  good: "bg-good-soft text-good-ink border-transparent",
  warn: "bg-warn-soft text-warn-ink border-transparent",
  serious: "bg-serious-soft text-serious-ink border-transparent",
  critical: "bg-critical-soft text-critical-ink border-transparent",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-[var(--axis)]",
  info: "bg-accent",
  good: "bg-good",
  warn: "bg-warn",
  serious: "bg-serious",
  critical: "bg-critical",
};

export function Pill({
  tone = "neutral",
  icon,
  dot,
  children,
  className = "",
  title,
}: {
  tone?: Tone;
  icon?: IconName;
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex h-6 max-w-full items-center gap-1 whitespace-nowrap rounded-full border px-2 text-xs font-medium ${TONE[tone]} ${className}`}
    >
      {icon ? <Icon name={icon} size={12} /> : dot ? <span className={`h-1.5 w-1.5 rounded-full ${DOT[tone]}`} aria-hidden /> : null}
      <span className="truncate">{children}</span>
    </span>
  );
}

const VERDICT_ICON: Record<Verdict, IconName> = { PASS: "check", FIX: "sparkle", HOLD: "pause", BLOCK: "ban" };

export function VerdictPill({ verdict, className }: { verdict: Verdict | null | undefined; className?: string }) {
  if (!verdict) {
    return (
      <Pill tone="neutral" icon="clock" className={className}>
        검수 전
      </Pill>
    );
  }
  const v = VERDICT[verdict];
  return (
    <Pill tone={v.tone} icon={VERDICT_ICON[verdict]} title={v.hint} className={className}>
      {verdict} · {v.label}
    </Pill>
  );
}

export function RunStatusPill({ status }: { status: RunStatus }) {
  const s = RUN_STATUS[status];
  return (
    <Pill tone={s.tone} dot>
      {s.label}
    </Pill>
  );
}

export function PublicationStatePill({ state }: { state: string }) {
  const s = publicationState(state);
  return (
    <Pill tone={s.tone} dot title={state}>
      {s.label}
    </Pill>
  );
}

export function AccountStatusPill({ status }: { status: AccountStatus }) {
  const s = ACCOUNT_STATUS[status];
  return (
    <Pill tone={s.tone} icon={status === "active" ? "check" : status === "cooldown" ? "clock" : "ban"}>
      {s.label}
    </Pill>
  );
}

export function RolePill({ role }: { role: Role }) {
  return (
    <Pill tone={role === "owner" ? "info" : "neutral"} icon="user">
      {ROLE_LABEL[role]} · {role}
    </Pill>
  );
}

/** Platform name with its fixed categorical swatch (identity never relies on colour alone). */
export function PlatformTag({ platform, className = "" }: { platform: string; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap text-sm text-ink ${className}`}>
      <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: platformColor(platform) }} aria-hidden />
      {PLATFORM_LABEL[platform as Platform] ?? platform}
    </span>
  );
}
