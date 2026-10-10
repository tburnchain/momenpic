"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { usePrincipal, useSystemStatus } from "@/lib/queries";
import { SETUP_DISMISS_KEY, SETUP_DONE_KEY, SETUP_NOTICE_KEY } from "@/lib/setup";

/**
 * Owners land on 운영 준비 (/setup) when readiness checks fail — once per browser session, and
 * never again after "다시 이동하지 않기". Renders nothing.
 */
export function SetupRedirect() {
  const router = useRouter();
  const { role } = usePrincipal();
  const isOwner = role === "owner";
  const status = useSystemStatus(isOwner);
  const fails = status.data?.summary.fail ?? 0;

  useEffect(() => {
    if (!isOwner || fails === 0) return;
    try {
      if (window.localStorage.getItem(SETUP_DISMISS_KEY) === "1") return;
      if (window.sessionStorage.getItem(SETUP_DONE_KEY) === "1") return;
      window.sessionStorage.setItem(SETUP_DONE_KEY, "1");
      window.sessionStorage.setItem(SETUP_NOTICE_KEY, "1");
    } catch {
      return;
    }
    router.replace("/setup");
  }, [isOwner, fails, router]);

  return null;
}
