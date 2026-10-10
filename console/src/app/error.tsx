"use client";

import { Button } from "@/components/ui/Button";
import { ErrorBanner } from "@/components/ui/Feedback";

export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="space-y-4">
      <ErrorBanner error={error} />
      <Button icon="refresh" onClick={reset}>
        화면 다시 불러오기
      </Button>
    </div>
  );
}
