import type { ReactNode } from "react";
import { DashboardTabs } from "@/components/dashboards/DashboardTabs";
import { PageHeader } from "@/components/ui/misc";

export default function DashboardsLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <PageHeader title="대시보드" description="§9.3 핵심 대시보드 — 머니 · 파이프라인 · 리스크 · 학습" />
      <DashboardTabs />
      {children}
    </>
  );
}
