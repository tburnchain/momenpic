import type { Metadata } from "next";
import { HoldReviewer } from "@/components/holds/HoldReviewer";
import { PageHeader } from "@/components/ui/misc";

export const metadata: Metadata = { title: "HOLD 큐" };

export default function HoldsPage() {
  return (
    <>
      <PageHeader
        title="HOLD 큐"
        description="M7 게이트가 사람 승인을 요구한 콘텐츠입니다. 한 건씩 빠르게 검토하세요 — 승인한 콘텐츠만 배포됩니다 (§8.4)."
      />
      <HoldReviewer />
    </>
  );
}
