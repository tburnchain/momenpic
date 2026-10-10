import type { Metadata } from "next";
import { PublicationsView } from "@/components/ops/PublicationsView";

export const metadata: Metadata = { title: "게시 현황" };

export default function Page() {
  return <PublicationsView />;
}
