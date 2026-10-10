import type { Metadata } from "next";
import { RunDetail } from "@/components/runs/RunDetail";

export const metadata: Metadata = { title: "실행 상세" };

export default async function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunDetail id={decodeURIComponent(id)} />;
}
