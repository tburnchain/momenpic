import type { Metadata } from "next";
import { SetupView } from "@/components/ops/SetupView";

export const metadata: Metadata = { title: "운영 준비" };

export default function Page() {
  return <SetupView />;
}
