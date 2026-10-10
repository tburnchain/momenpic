import type { Metadata } from "next";
import { Suspense } from "react";
import { SignupView } from "@/components/auth/AuthViews";

export const metadata: Metadata = { title: "회원가입" };

export default function Page() {
  return (
    <Suspense fallback={null}>
      <SignupView />
    </Suspense>
  );
}
