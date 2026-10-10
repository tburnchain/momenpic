import type { Metadata } from "next";
import { LearningView } from "@/components/dashboards/LearningView";

export const metadata: Metadata = { title: "학습 대시보드" };

export default function Page() {
  return <LearningView />;
}
