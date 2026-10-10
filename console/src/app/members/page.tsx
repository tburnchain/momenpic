import type { Metadata } from "next";
import { MembersView } from "@/components/admin/MembersView";

export const metadata: Metadata = { title: "회원 관리" };

export default function Page() {
  return <MembersView />;
}
