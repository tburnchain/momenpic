import type { Metadata } from "next";
import { SimulatorView } from "@/components/simulator/SimulatorView";

export const metadata: Metadata = { title: "수익 시뮬레이터" };

export default function Page() {
  return <SimulatorView />;
}
