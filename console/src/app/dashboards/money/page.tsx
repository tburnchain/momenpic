import type { Metadata } from "next";
import { MoneyView } from "@/components/dashboards/MoneyView";

export const metadata: Metadata = { title: "머니 대시보드" };

export default function Page() {
  return <MoneyView />;
}
