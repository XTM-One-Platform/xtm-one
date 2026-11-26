// SPDX-License-Identifier: Apache-2.0

/**
 * One CTEM gate's decision on the case page (#2464): its resolved record
 * (`GateDecisionRecord`) and, while it waits, the open decision on the unified
 * work-item decision card (dev-docs/human-gates.md), never a CTEM card. The
 * item is read by the record's own `work_item_id`, so a gate the backend adds
 * to `ctem_case_gate_records.GATE_ITEM_PREFIXES` shows its decision without a
 * correlation-key prefix written per section; a caller may give the key of an
 * older API that serves no id. A viewer who cannot read the item reads that the
 * decision is in the person's list; a gate with no decision of its own filed
 * (`decision_filed`, per gate) reads that nobody can answer it yet.
 */
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import type { CtemCaseEvent, CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import { useWorkItem, useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { mayAnswerCaseGate } from "@/lib/ctem-access";
import { waitsForAnswer } from "@/lib/work-item-context";
import type { WorkItem } from "@/types";
import { GATE_LABEL } from "./case-gates";
import { GateDecisionRecord, gateAnswererName } from "./GateDecisionRecord";

/**
 * The open decision item of a waiting gate: by the record's `work_item_id`,
 * else by `fallbackKey` (an API that names no item), read with the viewer's
 * own work-item visibility. Null while it does not wait or nothing is readable.
 */
export function useGateDecisionItem(
  record: Pick<CtemCaseGateRecord, "work_item_id"> | null,
  waiting: boolean,
  fallbackKey?: string | null,
): { item: WorkItem | null; isLoading: boolean; refetch: () => void } {
  const id = waiting ? (record?.work_item_id ?? null) : null;
  const byId = useWorkItem(id);
  const byKey = useWorkItems({ correlation_key: fallbackKey ?? undefined, open_only: true, limit: 1 }, waiting && !id && !!fallbackKey);
  if (!waiting) return { item: null, isLoading: false, refetch: () => undefined };
  if (id) {
    const found = byId.data && waitsForAnswer(byId.data) ? byId.data : null;
    return { item: found, isLoading: byId.isLoading, refetch: () => void byId.refetch() };
  }
  if (!fallbackKey) return { item: null, isLoading: false, refetch: () => undefined };
  return {
    item: (byKey.data ?? []).find(waitsForAnswer) ?? null,
    isLoading: byKey.isLoading,
    refetch: () => void byKey.refetch(),
  };
}

export function CaseGateDecision({
  record,
  history,
  personName,
  titled = false,
  testId,
}: {
  record: CtemCaseGateRecord;
  history: readonly CtemCaseEvent[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
  /** Name the gate above its record: a section that holds several gates, or the page's waiting card. */
  titled?: boolean;
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const waiting = record.status === "waiting";
  const { item, isLoading, refetch } = useGateDecisionItem(record, waiting);

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid={testId} data-gate={record.key}>
      <GateDecisionRecord
        record={record}
        title={titled ? t_i18n(GATE_LABEL[record.key] ?? "Gate") : undefined}
        byLabel={gateAnswererName(record, history, personName)}
        testId={`${testId}-record`}
      />
      {waiting && item && (
        <WorkItemDecisionCard
          item={item}
          inCase
          canAnswer={mayAnswerCaseGate(item, user, Boolean(isAdmin))}
          readOnlyNote={t_i18n("Only an administrator or a member of a group with View and decide can answer this decision.")}
          onAnswered={refetch}
        />
      )}
      {waiting && !item && !isLoading && record.decision_filed === false && (
        <p className="text-default-secondary" data-testid={`${testId}-not-filed`}>
          {t_i18n("No decision was filed for this gate, so nobody can answer it yet. The fleet files it the next time it runs this case.")}
        </p>
      )}
      {/* decision_filed is per gate (#2477 UX round 1): another gate's item never makes this one read filed. */}
      {waiting && !item && !isLoading && record.decision_filed !== false && (
        <p className="text-default-secondary" data-testid={`${testId}-elsewhere`}>
          {t_i18n("This gate waits for a person: the decision is in their Waiting for you list.")}
        </p>
      )}
    </div>
  );
}
