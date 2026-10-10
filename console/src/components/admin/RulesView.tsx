"use client";

import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { CopyButton, PageHeader } from "@/components/ui/misc";
import { Pill, VerdictPill } from "@/components/ui/Pill";
import { verdictFromText } from "@/lib/labels";
import { useRules } from "@/lib/queries";
import type { Rule } from "@/lib/schemas";

const GROUPS: Record<string, string> = {
  D: "대가성 표시 (Disclosure)",
  C: "주장·표현 (Claims)",
  R: "권리·유사도 (Rights)",
  S: "스팸·쿼터 (Spam)",
  A: "AI 생성물 표시",
  H: "사람 승인 (Human)",
};

function groupOf(r: Rule) {
  return r.id.split("-")[0]?.toUpperCase() ?? "?";
}

export function RulesView() {
  const q = useRules();
  const rules = q.data?.rules ?? [];
  const groups = Array.from(new Set(rules.map(groupOf)));

  return (
    <>
      <PageHeader
        title="규칙"
        description="M7 컴플라이언스 게이트 규칙 세트. 어떤 학습 결과도 게이트 규칙을 완화할 수 없습니다 (§8)."
        actions={q.data ? <Pill tone="info">rule_set {q.data.version}</Pill> : null}
      />
      <div className="space-y-5">
        <Notice title="읽기 전용">규칙 변경은 백엔드 rule_set 릴리스(회귀 테스트 100% 커버)로만 반영됩니다. 콘솔에서는 열람만 할 수 있습니다.</Notice>
        {q.isPending ? (
          <Card>
            <CardBody className="pt-5">
              <LoadingBlock rows={8} />
            </CardBody>
          </Card>
        ) : q.error ? (
          <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
        ) : (
          <>
            <Card aria-labelledby="rules-disclosure">
              <CardHeader id="rules-disclosure" title="표준 대가성 문구" description="모든 게시물 첫머리에 삽입 (2024-12-01 개정 추천·보증 심사지침 반영)" />
              <CardBody className="grid gap-3 md:grid-cols-2">
                <div className="rounded-lg border border-line p-3">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-muted">캡션·본문 첫 줄 (D-01, D-02)</span>
                    <CopyButton text={q.data.disclosure.text} />
                  </div>
                  <p className="text-sm text-ink">{q.data.disclosure.text}</p>
                </div>
                <div className="rounded-lg border border-line p-3">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-muted">영상 첫 3초 상단 오버레이 (D-03)</span>
                    <CopyButton text={q.data.disclosure.video_text} />
                  </div>
                  <p className="text-sm text-ink">{q.data.disclosure.video_text}</p>
                </div>
              </CardBody>
            </Card>

            {!rules.length ? (
              <Card>
                <EmptyState icon="shield" title="규칙이 없습니다" />
              </Card>
            ) : (
              groups.map((g) => (
                <Card key={g} aria-labelledby={`rules-${g}`}>
                  <CardHeader id={`rules-${g}`} title={GROUPS[g] ?? g} />
                  <CardBody>
                    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
                      <table className="w-full min-w-[620px] text-left text-sm">
                        <caption className="sr-only">{GROUPS[g] ?? g} 규칙</caption>
                        <thead className="border-b border-line text-[13px] text-muted">
                          <tr>
                            <th scope="col" className="w-20 py-2 pr-3 font-medium">ID</th>
                            <th scope="col" className="py-2 pr-3 font-medium">검사</th>
                            <th scope="col" className="py-2 pr-3 font-medium">방법</th>
                            <th scope="col" className="w-56 py-2 font-medium">실패 시</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rules
                            .filter((r) => groupOf(r) === g)
                            .map((r) => {
                              const v = verdictFromText(r.on_fail);
                              return (
                                <tr key={r.id} className="border-b border-line align-top last:border-0">
                                  <td className="py-2.5 pr-3 font-mono text-[13px] font-semibold">{r.id}</td>
                                  <td className="py-2.5 pr-3 text-ink">{r.check}</td>
                                  <td className="py-2.5 pr-3 text-ink-2">{r.method}</td>
                                  <td className="py-2.5">
                                    {v ? <VerdictPill verdict={v} /> : null}
                                    <div className="mt-1 text-xs text-muted">{r.on_fail}</div>
                                  </td>
                                </tr>
                              );
                            })}
                        </tbody>
                      </table>
                    </div>
                  </CardBody>
                </Card>
              ))
            )}
          </>
        )}
      </div>
    </>
  );
}
