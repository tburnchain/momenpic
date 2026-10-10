import type { Metadata } from "next";
import { ManualQueueView } from "@/components/admin/ManualQueueView";

export const metadata: Metadata = { title: "수동 게시" };

export default function Page() {
  return <ManualQueueView />;
}
