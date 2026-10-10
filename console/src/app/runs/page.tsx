import type { Metadata } from "next";
import { RunsList } from "@/components/runs/RunsList";
import { PageHeader } from "@/components/ui/misc";

export const metadata: Metadata = { title: "실행" };

export default function RunsPage() {
  return (
    <>
      <PageHeader title="실행" description="명령 실행(run) 목록 — 상태, 비용, 소요 시간. 진행 중인 실행은 자동으로 갱신됩니다." />
      <RunsList />
    </>
  );
}
