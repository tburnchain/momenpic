import type { Metadata } from "next";
import { ContentDetail } from "@/components/ops/ContentDetail";

export const metadata: Metadata = { title: "콘텐츠 상세" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ContentDetail id={decodeURIComponent(id)} />;
}
