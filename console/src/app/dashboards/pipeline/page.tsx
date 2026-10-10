import type { Metadata } from "next";
import { PipelineView } from "@/components/dashboards/PipelineView";

export const metadata: Metadata = { title: "파이프라인 대시보드" };

export default function Page() {
  return <PipelineView />;
}
