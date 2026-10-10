import type { Metadata } from "next";
import { RulesView } from "@/components/admin/RulesView";

export const metadata: Metadata = { title: "규칙" };

export default function Page() {
  return <RulesView />;
}
