import { ButtonLink } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/Feedback";

export default function NotFound() {
  return (
    <EmptyState
      icon="alert"
      title="페이지를 찾을 수 없습니다"
      action={
        <ButtonLink href="/" variant="primary" icon="terminal">
          명령 화면으로
        </ButtonLink>
      }
    >
      주소를 확인하거나 왼쪽 메뉴에서 이동하세요.
    </EmptyState>
  );
}
