"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState, ErrorBanner, LoadingBlock, Notice } from "@/components/ui/Feedback";
import { Field, Select, TextInput } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/misc";
import { Pill } from "@/components/ui/Pill";
import { useToast } from "@/components/ui/Toast";
import { describeCron, isValidCron } from "@/lib/cron";
import { useCreateSchedule, useDeleteSchedule, useSchedules, usePrincipal } from "@/lib/queries";
import { ALLOWED_ROLES } from "@/lib/rbac";
import type { Schedule } from "@/lib/schemas";

const PRESETS = [
  { cron: "0 9 * * *", label: "매일 09:00" },
  { cron: "0 21 * * *", label: "매일 21:00" },
  { cron: "0 8 * * 1-5", label: "평일 08:00" },
  { cron: "0 8 * * 1", label: "매주 월요일 08:00" },
  { cron: "custom", label: "직접 입력" },
];

function CreateForm() {
  const create = useCreateSchedule();
  const toast = useToast();
  const [text, setText] = useState("");
  const [preset, setPreset] = useState(PRESETS[0].cron);
  const [custom, setCustom] = useState("");
  const cron = preset === "custom" ? custom.trim() : preset;
  const cronOk = isValidCron(cron);
  const textOk = text.trim().length >= 2;

  return (
    <form
      className="grid gap-4 md:grid-cols-[minmax(0,1fr)_14rem_auto] md:items-end"
      onSubmit={(e) => {
        e.preventDefault();
        if (!cronOk || !textOk) return;
        create.mutate(
          { text: text.trim(), cron },
          {
            onSuccess: () => {
              toast("반복 예약을 저장했습니다");
              setText("");
            },
          },
        );
      }}
    >
      <Field label="반복 실행할 명령" htmlFor="sch-text" hint="실행 시점마다 이 명령으로 계획을 만들고 승인 규칙을 동일하게 적용합니다.">
        <TextInput id="sch-text" value={text} onChange={(e) => setText(e.target.value)} placeholder="차량용품 10개 글로 만들어서 스레드에 올려줘" maxLength={500} />
      </Field>
      <div className="space-y-2">
        <Field label="주기 (KST)" htmlFor="sch-preset">
          <Select id="sch-preset" value={preset} onChange={(e) => setPreset(e.target.value)}>
            {PRESETS.map((p) => (
              <option key={p.cron} value={p.cron}>
                {p.label}
              </option>
            ))}
          </Select>
        </Field>
        {preset === "custom" ? (
          <Field label="cron 식" htmlFor="sch-cron" error={custom && !cronOk ? "5개 필드 cron 식을 입력하세요 (분 시 일 월 요일)" : null} hint={cronOk ? describeCron(cron) : "예: 30 7 * * 1-5"}>
            <TextInput id="sch-cron" value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="0 9 * * *" className="font-mono" spellCheck={false} aria-invalid={!!custom && !cronOk} />
          </Field>
        ) : null}
      </div>
      <Button type="submit" variant="primary" icon="plus" loading={create.isPending} disabled={!cronOk || !textOk} className="md:mb-[22px]">
        예약 추가
      </Button>
      {create.error ? (
        <div className="md:col-span-3">
          <ErrorBanner error={create.error} compact />
        </div>
      ) : null}
    </form>
  );
}

export function SchedulesView() {
  const q = useSchedules();
  const del = useDeleteSchedule();
  const toast = useToast();
  const { can } = usePrincipal();
  const manage = can("manageSchedules");
  const [confirm, setConfirm] = useState<Schedule | null>(null);
  const items = q.data?.items ?? [];

  return (
    <>
      <PageHeader title="예약" description="“매일 오전 9시에 차량용품 10개” 같은 반복 명령을 스케줄 워크플로로 저장합니다 (§5.2). cron은 KST 기준입니다." />
      <div className="space-y-5">
        {manage ? (
          <Card aria-labelledby="sch-new">
            <CardHeader id="sch-new" title="새 반복 예약" />
            <CardBody>
              <CreateForm />
            </CardBody>
          </Card>
        ) : q.data ? (
          <Notice>예약 추가·중지는 {ALLOWED_ROLES.manageSchedules}만 할 수 있습니다.</Notice>
        ) : null}

        <Card aria-labelledby="sch-list">
          <CardHeader id="sch-list" title={`예약 목록 (${items.length})`} />
          <CardBody>
            {q.isPending ? (
              <LoadingBlock rows={4} />
            ) : q.error ? (
              <ErrorBanner error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
            ) : !items.length ? (
              <EmptyState icon="calendar" title="반복 예약이 없습니다">
                명령 화면에서 “매일 9시에 …”처럼 입력하거나 위 양식으로 추가하세요.
              </EmptyState>
            ) : (
              <ul className="divide-y divide-line">
                {items.map((s) => (
                  <li key={s.id} className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:gap-4">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-ink">{s.raw_command}</p>
                      <p className="mt-0.5 text-xs text-muted">
                        <span className="font-semibold text-ink-2">{describeCron(s.cron)}</span> · <code className="font-mono">{s.cron}</code>
                        {s.created_by ? ` · ${s.created_by}` : ""}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {s.active ? (
                        <Pill tone="good" icon="check">
                          활성
                        </Pill>
                      ) : (
                        <Pill tone="neutral" icon="pause">
                          비활성
                        </Pill>
                      )}
                      {manage && s.active ? (
                        <Button size="sm" variant="ghost" icon="pause" onClick={() => setConfirm(s)} aria-label={`예약 중지: ${s.raw_command}`}>
                          중지
                        </Button>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>

      <Dialog
        open={!!confirm}
        onClose={() => setConfirm(null)}
        title="반복 예약을 중지할까요?"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(null)}>
              취소
            </Button>
            <Button
              variant="danger"
              icon="pause"
              loading={del.isPending}
              onClick={() =>
                confirm &&
                del.mutate(confirm.id, {
                  onSuccess: () => {
                    toast("반복 예약을 중지했습니다", "info");
                    setConfirm(null);
                  },
                })
              }
            >
              중지
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink">{confirm?.raw_command}</p>
        <p className="mt-1 text-xs text-muted">
          {confirm ? describeCron(confirm.cron) : ""} — 예약은 비활성으로 남고(이력 보존), 이미 실행된 run에는 영향이 없습니다.
        </p>
        {del.error ? <ErrorBanner error={del.error} compact className="mt-3" /> : null}
      </Dialog>
    </>
  );
}
