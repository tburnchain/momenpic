import type { Metadata } from "next";
import { AccountsView } from "@/components/admin/AccountsView";

export const metadata: Metadata = { title: "계정" };

export default function Page() {
  return <AccountsView />;
}
