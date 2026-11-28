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
 * Every card rendered in place sits in `GateDecisionAnchor`, so the page's
 * "Waits for a decision" line and the Worked-by card point to it in page
 * instead of a link away (one way to answer a decision).
 */
import { useEffect, useRef, type ReactNode } from "react";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import type { CtemCaseEvent, CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import { useWorkItem, useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { mayAnswerCaseGate } from "@/lib/ctem-access";
import { waitsForAnswer } from "@/lib/work-item-context";
import type { WorkItem } from "@/types";
import { GATE_LABEL } from "./case-gates";
import { CollapseFrame, GateApplyingNotice, useCollapseOut, useGateApplyingState } from "./GateApplyingNotice";
import { GateDecisionRecord, gateAnswererName } from "./GateDecisionRecord";
import { clearGateAnswer, itemAnswer, recordGateAnswer, recordGatePhase, useGateAnswer, type GateAnswer, type GateApplying } from "./gate-applying";

/**
 * The answer a waiting gate already has (#2746): the one this page sent, else
 * the one its work item carries (answered elsewhere, or before a reload, while
 * the case still reads the gate waiting; open or done, #2757 UX round 2 B1),
 * kept in the page's store so every reader of
 * the gate (its line, the Worked-by row, the card) sees it. Null otherwise; a
 * gate that no longer waits forgets it, and a kept answer older than the gate's
 * current ask (an answer to an earlier ask of the same item, #2779) is dropped,
 * as `itemAnswer` drops one the item carries.
 */
export function useGateAnswerState(
  record: (Pick<CtemCaseGateRecord, "work_item_id"> & Partial<Pick<CtemCaseGateRecord, "asked_at">>) | null,
  waiting: boolean,
): GateAnswer | null {
  const id = record?.work_item_id ?? null;
  const stored = useGateAnswer(id);
  const asked = record?.asked_at ? Date.parse(record.asked_at) : Number.NaN;
  const stale = !!stored && Number.isFinite(asked) && stored.at < asked;
  const kept = stale ? null : stored;
  const raw = useWorkItem(waiting ? id : null);
  const done = waiting && !kept ? itemAnswer(raw.data ?? null, record?.asked_at) : null;
  const answeredAgain = !!done;
  useEffect(() => {
    if (id && done) recordGateAnswer(id, done.label, done.at);
  }, [id, done?.label, done?.at]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    // A newer answer the item carries replaces a stale one above; a stale one with nothing newer is cleared.
    if (!waiting || (stale && !answeredAgain)) clearGateAnswer(id);
  }, [id, waiting, stale, answeredAgain]);
  return waiting ? (kept ?? done) : null;
}

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

/** The in-page anchor of a gate's decision card, one per gate key. */
function gateDecisionAnchorId(gate: string): string {
  return `ctem-gate-decision-${gate.replace(/[^a-z0-9]+/gi, "-")}`;
}

/** Scroll to a gate's decision card on the page and focus it; false when the page does not show it. */
export function focusGateDecision(gate: string): boolean {
  const el = document.getElementById(gateDecisionAnchorId(gate));
  if (!el) return false;
  el.scrollIntoView?.({ behavior: "smooth", block: "center" });
  el.focus({ preventScroll: true });
  return true;
}

/**
 * Where a gate's decision card sits on the page relative to `from` (the control
 * that points to it): "up" when the card comes before it, "down" when after,
 * null when the page does not show the card. The control's icon says what is
 * true: the page's waiting card is above the Worked-by row, a section card below.
 */
export function gateDecisionDirection(gate: string, from: Element | null): "up" | "down" | null {
  const el = document.getElementById(gateDecisionAnchorId(gate));
  if (!el || !from) return null;
  return el.compareDocumentPosition(from) & Node.DOCUMENT_POSITION_FOLLOWING ? "up" : "down";
}

/** The wrapper of a decision card rendered in place: the target of "Answer on this page". */
export function GateDecisionAnchor({ gate, children }: { gate: string; children: ReactNode }) {
  return (
    <div id={gateDecisionAnchorId(gate)} tabIndex={-1} className="scroll-mt-4 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus" data-testid="ctem-gate-decision-anchor" data-gate={gate}>
      {children}
    </div>
  );
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
  const answer = useGateAnswerState(record, waiting);
  // One read of the phase for the chip and the notice, so they never disagree (UX round 1 on #2757, B1).
  const applyingState = useGateApplyingState(waiting && !!record.work_item_id, answer);
  const applying = !!applyingState;
  // The same phase for the waiting card's title as for this gate's chip and notice (UX round 3 on #2757, B1).
  const phase = applyingState?.phase ?? null;
  useEffect(() => {
    recordGatePhase(record.work_item_id, phase);
  }, [record.work_item_id, phase]);
  // Kept for the collapse once the loop applied it: the notice folds away instead of jumping.
  const lastState = useRef<GateApplying | null>(null);
  if (applyingState) lastState.current = applyingState;
  const notice = useCollapseOut(applying);

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid={testId} data-gate={record.key}>
      <GateDecisionRecord
        record={record}
        title={titled ? (record.title || t_i18n(GATE_LABEL[record.key] ?? "Gate")) : undefined}
        byLabel={gateAnswererName(record, history, personName)}
        applying={applyingState?.phase ?? null}
        testId={`${testId}-record`}
      />
      {notice.render && record.work_item_id && (
        <CollapseFrame closing={notice.closing} testId={`${testId}-applying-frame`}>
          <GateApplyingNotice workItemId={record.work_item_id} state={applyingState ?? lastState.current} testId={`${testId}-applying`} />
        </CollapseFrame>
      )}
      {waiting && !applying && item && (
        <GateDecisionAnchor gate={record.key}>
          <WorkItemDecisionCard
            item={item}
            inCase
            canAnswer={mayAnswerCaseGate(item, user, Boolean(isAdmin))}
            readOnlyNote={t_i18n("Only an administrator or a member of a group with View and decide can answer this decision.")}
            onAnswered={refetch}
            onAnswerSent={({ label }) => recordGateAnswer(item.id, label)}
          />
        </GateDecisionAnchor>
      )}
      {waiting && !applying && !item && !isLoading && record.decision_filed === false && (
        <p className="text-default-secondary" data-testid={`${testId}-not-filed`}>
          {t_i18n("No decision was filed for this gate, so nobody can answer it yet. The fleet files it the next time it runs this case.")}
        </p>
      )}
      {/* decision_filed is per gate (#2477 UX round 1): another gate's item never makes this one read filed. */}
      {waiting && !applying && !item && !isLoading && record.decision_filed !== false && (
        <p className="text-default-secondary" data-testid={`${testId}-elsewhere`}>
          {t_i18n("This gate waits for a person: the decision is in their Waiting for you list.")}
        </p>
      )}
    </div>
  );
}
