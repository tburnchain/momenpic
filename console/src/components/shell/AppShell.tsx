"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { IconButton } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/Feedback";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Pill } from "@/components/ui/Pill";
import { MOCK_MODE } from "@/lib/config";
import { isApiError } from "@/lib/errors";
import { ROLE_LABEL } from "@/lib/labels";
import { useAuthConfig, useHolds, useLogout, useManualQueue, useMembers, usePrincipal } from "@/lib/queries";
import { AlertsBell } from "./AlertsBell";
import { SettingsDialog } from "./SettingsDialog";

interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  badge?: "holds" | "manual" | "members";
  /** Shown only to roles with this permission. */
  admin?: boolean;
  /** Path prefix that marks the item active (defaults to href). */
  match?: string;
}

/** Ordered for the daily operator flow (15 min/day: command → HOLD → publications → numbers). */
const NAV: Array<{ group: string; items: NavItem[] }> = [
  {
    group: "일일 운영",
    items: [
      { href: "/studio", label: "스튜디오", icon: "film" },
      { href: "/", label: "명령", icon: "terminal" },
      { href: "/holds", label: "HOLD 큐", icon: "inbox", badge: "holds" },
      { href: "/publications", label: "게시 현황", icon: "calendar" },
      { href: "/dashboards/money", label: "대시보드", icon: "chart", match: "/dashboards" },
      { href: "/runs", label: "실행", icon: "play" },
    ],
  },
  {
    group: "채널",
    items: [
      { href: "/accounts", label: "계정", icon: "users" },
      { href: "/schedules", label: "예약", icon: "clock" },
      { href: "/manual", label: "수동 게시", icon: "send", badge: "manual" },
    ],
  },
  {
    group: "도구 · 설정",
    items: [
      { href: "/simulator", label: "수익 시뮬레이터", icon: "calculator" },
      { href: "/setup", label: "운영 준비", icon: "check" },
      { href: "/settings", label: "설정", icon: "settings" },
      { href: "/members", label: "회원 관리", icon: "user", badge: "members", admin: true },
      { href: "/rules", label: "규칙", icon: "shield" },
      { href: "/audit", label: "감사 로그", icon: "scroll" },
    ],
  },
];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 rounded-lg px-1 py-1">
      <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-accent-fill text-[13px] font-bold text-white" aria-hidden>
        AV
      </span>
      <span className="leading-tight">
        <span className="block text-[15px] font-bold tracking-tight text-ink">ACVM 콘솔</span>
        <span className="block whitespace-nowrap text-[11px] text-muted">제휴 콘텐츠 자판기</span>
      </span>
    </Link>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  // Shell-level polling only once the principal is known (avoids a burst of 401s before sign-in).
  const principal = usePrincipal();
  const authed = !!principal.me;
  const isAdmin = principal.can("admin");
  const holds = useHolds(authed);
  const manual = useManualQueue(authed);
  const members = useMembers(authed && isAdmin);
  const counts = {
    holds: holds.data?.items.length ?? 0,
    manual: manual.data?.items.length ?? 0,
    members: members.data?.pending ?? 0,
  };

  return (
    <nav aria-label="주 메뉴" className="space-y-5">
      {NAV.map((g) => (
        <div key={g.group}>
          <p className="mb-1.5 px-3 text-[11px] font-semibold uppercase tracking-wider text-muted">{g.group}</p>
          <ul className="space-y-0.5">
            {g.items.filter((item) => !item.admin || isAdmin).map((item) => {
              const active = isActive(pathname, item.match ?? item.href);
              const count = item.badge ? counts[item.badge] : 0;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={`flex h-9 items-center gap-2.5 rounded-lg px-3 text-sm transition-colors ${
                      active ? "bg-accent-soft font-semibold text-accent-ink" : "text-ink-2 hover:bg-surface-2 hover:text-ink"
                    }`}
                  >
                    <Icon name={item.icon} size={16} />
                    <span className="flex-1">{item.label}</span>
                    {count > 0 ? (
                      <span
                        className={`min-w-5 rounded-full px-1.5 text-center text-[11px] font-semibold leading-5 ${
                          item.badge === "holds" ? "bg-serious-soft text-serious-ink" : "bg-surface-3 text-ink-2"
                        }`}
                        aria-label={`${count}건 대기`}
                      >
                        {count}
                      </span>
                    ) : null}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** Display name for the signed-in member (name, else the e-mail's local part). */
export function displayName(me: { name?: string | null; email?: string | null; subject: string }): string {
  return me.name?.trim() || (me.email ?? me.subject).split("@")[0];
}

function useSignOut() {
  const logout = useLogout();
  const router = useRouter();
  return {
    pending: logout.isPending,
    run: () => logout.mutate(undefined, { onSettled: () => router.replace("/login") }),
  };
}

function PrincipalCard({ onSettings }: { onSettings: () => void }) {
  const { me, query } = usePrincipal();
  const signOut = useSignOut();
  const member = me?.auth === "session";
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-line bg-surface-2 p-2.5">
      <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-3 text-xs font-bold uppercase text-ink-2" aria-hidden>
        {me ? displayName(me).slice(0, 1) : "?"}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium text-ink" title={me?.email ?? me?.subject}>
          {me ? displayName(me) : query.isPending ? "확인 중…" : "로그인 필요"}
        </p>
        <p className="truncate text-[11px] text-muted" title={me?.email ?? undefined}>
          {me ? `${ROLE_LABEL[me.role]} · ${me.email ?? me.subject}` : ""}
        </p>
      </div>
      <IconButton icon="settings" label="계정·표시 설정" size="sm" onClick={onSettings} />
      {member ? <IconButton icon="logout" label="로그아웃" size="sm" onClick={signOut.run} disabled={signOut.pending} /> : null}
    </div>
  );
}

const AUTH_PAGES = ["/login", "/signup"];

/** Session mode: an unauthenticated visit goes to the login page (and comes back afterwards). */
function useLoginRedirect(isAuthPage: boolean) {
  const cfg = useAuthConfig();
  const { query } = usePrincipal();
  const router = useRouter();
  const pathname = usePathname();
  const err = query.error;
  const unauthenticated = isApiError(err) && err.kind === "http" && err.status === 401;
  const session = !MOCK_MODE && cfg.data?.mode === "session";
  useEffect(() => {
    if (session && unauthenticated && !isAuthPage) {
      const next = typeof window !== "undefined" ? window.location.pathname + window.location.search : pathname;
      router.replace(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [session, unauthenticated, isAuthPage, pathname, router]);
  return session && unauthenticated && !isAuthPage;
}

/** Surfaces auth / connectivity problems detected by GET /me once, above every page. */
function GlobalStatus() {
  const { query } = usePrincipal();
  const err = query.error;
  const cfg = useAuthConfig();
  if (!err || !isApiError(err)) return null;
  if (!(err.isAuth || err.kind === "network" || err.kind === "timeout")) return null;
  if (cfg.data?.mode === "session" && err.status === 401) return null; // redirecting to /login
  return (
    <div className="mx-auto max-w-[1400px] px-4 pt-4 sm:px-6 lg:px-8">
      <ErrorBanner error={err} onRetry={() => void query.refetch()} retrying={query.isFetching} compact />
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [drawer, setDrawer] = useState(false);
  const [settings, setSettings] = useState(false);
  const { me } = usePrincipal();
  const pathname = usePathname();
  const isAuthPage = AUTH_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const redirecting = useLoginRedirect(isAuthPage);

  if (isAuthPage) return <>{children}</>;
  if (redirecting) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-sm text-muted" role="status">
        로그인 화면으로 이동 중…
      </div>
    );
  }

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[252px_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded-lg bg-accent-fill px-3 py-2 text-white focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        본문으로 건너뛰기
      </a>

      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-dvh flex-col gap-5 border-r border-line bg-surface px-3 py-4 lg:flex">
        <div className="flex items-center justify-between px-1">
          <Brand />
          {MOCK_MODE ? (
            <Pill tone="warn" title="NEXT_PUBLIC_ACVM_MOCK=1">
              MOCK
            </Pill>
          ) : null}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          <NavList />
        </div>
        <PrincipalCard onSettings={() => setSettings(true)} />
      </aside>

      <div className="min-w-0">
        {/* Mobile top bar */}
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-surface/90 px-2 backdrop-blur lg:hidden">
          <IconButton icon="menu" label="메뉴 열기" onClick={() => setDrawer(true)} aria-expanded={drawer} aria-controls="mobile-nav" />
          <div className="flex-1">
            <Brand />
          </div>
          {MOCK_MODE ? <Pill tone="warn">MOCK</Pill> : null}
          {me ? <span className="hidden text-xs text-muted min-[420px]:inline">{ROLE_LABEL[me.role]}</span> : null}
          <AlertsBell />
          <IconButton icon="settings" label="연결·표시 설정" onClick={() => setSettings(true)} />
        </header>

        {/* Desktop top bar */}
        <header className="sticky top-0 z-30 hidden h-12 items-center justify-end gap-2 border-b border-line bg-page/85 px-6 backdrop-blur lg:flex">
          {me ? (
            <span className="text-xs text-muted" title={me.email ?? me.subject}>
              {displayName(me)} · {ROLE_LABEL[me.role]}
            </span>
          ) : null}
          <AlertsBell />
        </header>

        <GlobalStatus />
        <main id="main" tabIndex={-1} className="mx-auto max-w-[1400px] px-4 pb-24 pt-5 outline-none sm:px-6 lg:px-8 lg:pb-12 lg:pt-7">
          {children}
        </main>
      </div>

      {/* Mobile drawer */}
      {drawer ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="주 메뉴" id="mobile-nav">
          <button type="button" aria-label="메뉴 닫기" className="absolute inset-0 bg-black/40" onClick={() => setDrawer(false)} />
          <div
            className="absolute inset-y-0 left-0 flex w-[min(86vw,300px)] flex-col gap-4 bg-surface px-3 py-4 shadow-2xl"
            onKeyDown={(e) => e.key === "Escape" && setDrawer(false)}
          >
            <div className="flex items-center justify-between">
              <Brand />
              <IconButton icon="x" label="메뉴 닫기" onClick={() => setDrawer(false)} autoFocus />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <NavList onNavigate={() => setDrawer(false)} />
            </div>
            <PrincipalCard
              onSettings={() => {
                setDrawer(false);
                setSettings(true);
              }}
            />
          </div>
        </div>
      ) : null}

      <SettingsDialog open={settings} onClose={() => setSettings(false)} />
    </div>
  );
}
