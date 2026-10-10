import type { Metadata } from "next";
import { RiskView } from "@/components/dashboards/RiskView";

export const metadata: Metadata = { title: "리스크 대시보드" };

export default function Page() {
  return <RiskView />;
}
