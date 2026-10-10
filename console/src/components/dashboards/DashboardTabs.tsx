"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/dashboards/money", label: "머니" },
  { href: "/dashboards/pipeline", label: "파이프라인" },
  { href: "/dashboards/risk", label: "리스크" },
  { href: "/dashboards/learning", label: "학습" },
];

export function DashboardTabs() {
  const pathname = usePathname();
  return (
    <nav aria-label="대시보드 보기" className="-mx-1 mb-5 overflow-x-auto px-1">
      <ul className="inline-flex gap-1 rounded-xl border border-line bg-surface-2 p-1">
        {TABS.map((t) => {
          const active = pathname === t.href;
          return (
            <li key={t.href}>
              <Link
                href={t.href}
                aria-current={active ? "page" : undefined}
                className={`inline-flex h-8 items-center rounded-lg px-3.5 text-sm font-medium whitespace-nowrap transition-colors ${
                  active ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"
                }`}
              >
                {t.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
