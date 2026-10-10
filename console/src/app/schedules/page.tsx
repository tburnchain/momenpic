import type { Metadata } from "next";
import { SchedulesView } from "@/components/admin/SchedulesView";

export const metadata: Metadata = { title: "예약" };

export default function Page() {
  return <SchedulesView />;
}
