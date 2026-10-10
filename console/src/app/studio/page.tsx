import type { Metadata } from "next";
import { Suspense } from "react";
import { StudioView } from "@/components/studio/StudioView";
import { LoadingBlock } from "@/components/ui/Feedback";

export const metadata: Metadata = { title: "쇼츠 스튜디오" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={8} />}>
      <StudioView />
    </Suspense>
  );
}
