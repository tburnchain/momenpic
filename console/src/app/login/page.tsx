import type { Metadata } from "next";
import { Suspense } from "react";
import { LoginView } from "@/components/auth/AuthViews";

export const metadata: Metadata = { title: "로그인" };

export default function Page() {
  return (
    <Suspense fallback={null}>
      <LoginView />
    </Suspense>
  );
}
