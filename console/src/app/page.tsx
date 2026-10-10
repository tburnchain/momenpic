import type { Metadata } from "next";
import { CommandConsole } from "@/components/command/CommandConsole";
import { SetupRedirect } from "@/components/ops/SetupRedirect";
import { PageHeader } from "@/components/ui/misc";

export const metadata: Metadata = { title: { absolute: "명령 · ACVM" } };

export default function CommandPage() {
  return (
    <>
      <PageHeader
        title="명령"
        description="한 줄 명령을 실행 계획(DAG)으로 바꿔 미리보고 승인합니다. LLM은 계획만 만들고, 실행은 결정적 코드가 맡습니다 (§5)."
      />
      <SetupRedirect />
      <CommandConsole />
    </>
  );
}
