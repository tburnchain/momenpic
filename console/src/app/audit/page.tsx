import type { Metadata } from "next";
import { AuditView } from "@/components/admin/AuditView";

export const metadata: Metadata = { title: "감사 로그" };

export default function Page() {
  return <AuditView />;
}
