// SPDX-License-Identifier: Apache-2.0

/**
 * The decision record of one CTEM gate (#1518): a status chip and a compact
 * label/value grid (who decided, when, the option by its label, their comment,
 * how long it waited, the channel, the decision and what ran next), read from
 * the resolved record `GET /ctem-cases/{id}` serves (`gates`,
 * `ctem_case_gate_records.resolve`). An answered decision always reads as
 * answered, whether or not the case recorded the answer yet. Used by Analyst
 * review, Validation and Remediation decision, and exported for the History
 * timeline. Never prints a raw id: a person the viewer may not see is "a person".
 */
import type * as React from "react";
import { Link as RouterLink } from "react-router-dom";
import { Link } from "@filigran/design-system";
import { ToneBadge, type HotTone } from "@/components/ui/tone-badge";
import type { CtemCaseGateRecipient, CtemCaseGateRecord, CtemCaseGateStatus } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { workItemPath } from "@/lib/task-routes";
import { CASE_EVENT_LABEL } from "./case-events";
import { GATE_LABEL, GATE_OUTCOME_LABEL, GATE_REASON_LABEL, isGateOutcome } from "./case-gates";
import { CASE_STATE_LABEL } from "./cases-shared";

/**
 * What the chip says: the record's status, an answer read by the meaning of
 * the option chosen (approved, rejected, deferred), "answered" only when the
 * option has no known meaning.
 */
export type GateChip = Exclude<CtemCaseGateStatus, "answered"> | "approved" | "rejected" | "deferred" | "answered";

/** A chip in words. @i18n-keys */
const GATE_CHIP_LABEL = {
  waiting: "Waiting",
  answered: "Answered",
  approved: "Approved",
  rejected: "Rejected",
  deferred: "Deferred",
  passed_automatically: "Passed automatically",
  expired: "Expired",
  cancelled: "Cancelled",
} as const satisfies Record<GateChip, string>;

const GATE_CHIP_TONE: Record<GateChip, HotTone> = {
  waiting: "yellow",
  answered: "neutral",
  approved: "green",
  rejected: "red",
  deferred: "neutral",
  passed_automatically: "blue",
  expired: "orange",
  cancelled: "neutral",
};

/** The first word of an option key or label that tells its meaning. */
const ANSWER_MEANING: ReadonlyArray<[RegExp, "approved" | "rejected" | "deferred"]> = [
  [/^(approve[ds]?|approval|accept(ed|s)?|allow(ed|s)?|yes|go|confirm(ed|s)?)$/, "approved"],
  [/^(reject(ed|s)?|decline[ds]?|deny|denied|refuse[ds]?|no|block(ed|s)?)$/, "rejected"],
  [/^(defer(red|s)?|postpone[ds]?|later|snooze[ds]?|wait|hold)$/, "deferred"],
];

function meaningOf(text: string | null | undefined): "approved" | "rejected" | "deferred" | null {
  const word = (text ?? "").trim().toLowerCase().split(/[^a-z]+/)[0] ?? "";
  return ANSWER_MEANING.find(([pattern]) => pattern.test(word))?.[1] ?? null;
}

/** The chip of a record: an answer by the meaning of its option key, else its label, else "answered". */
export function gateChipOf(record: Pick<CtemCaseGateRecord, "status" | "option">): GateChip {
  if (record.status !== "answered") return record.status;
  return meaningOf(record.option?.key) ?? meaningOf(record.option?.label) ?? "answered";
}

/** The channel an answer came from (`decision_router.SURFACES`). @i18n-keys */
const GATE_SURFACES = {
  router: "XTM One",
  web: "XTM One",
  inbox: "Inbox",
  bell: "Notification bell",
  push: "Push notification",
  mail: "Email",
  email: "Email",
  teams_card: "Teams card",
  teams_chat: "Teams chat",
  teams_channel: "Teams channel",
  slack_card: "Slack card",
  whatsapp_card: "WhatsApp card",
  google_chat_card: "Google Chat card",
  google_chat: "Google Chat",
} as const;

const GATE_SURFACE_LABEL: Readonly<Record<string, string>> = GATE_SURFACES;

/** Why a gate passed without a person, the kinds outside GATE_REASON_LABEL. @i18n-keys */
const EXTRA_REASON_LABEL = {
  fleet_level: "The fleet runs this gate without a person",
} as const;

/** Who a decision was asked of when the viewer may not see the name. @i18n-keys */
const RECIPIENT_LABEL = {
  user: "A person",
  group: "A group",
  company: "Everyone in the company",
  admins: "Administrators",
} as const;

/** The record of one gate key among the case's `gates`, or null (an API older than #1524 serves none). */
export function gateRecordOf(gates: readonly CtemCaseGateRecord[] | undefined, key: string): CtemCaseGateRecord | null {
  return gates?.find((g) => g.key === key) ?? null;
}

/** The reason of a policy pass in words (the catalog key and its values), or null. */
export function gateRecordReason(reason: CtemCaseGateRecord["reason"]): { id: string; values?: Record<string, string> } | null {
  if (!reason || reason.kind === "person") return null;
  if (reason.kind === "runbook") return { id: GATE_REASON_LABEL.runbook, values: { runbook: reason.runbook ?? "" } };
  if (reason.kind === "within_threshold" || reason.kind === "above_threshold" || reason.kind === "loss_unknown") {
    return { id: GATE_REASON_LABEL[reason.kind] };
  }
  return { id: EXTRA_REASON_LABEL[reason.kind] };
}

/** The largest two units of a wait, e.g. [2, "hour"], [5, "minute"]; seconds only under a minute. */
export function waitParts(seconds: number): Array<[number, "day" | "hour" | "minute" | "second"]> {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return [[total, "second"]];
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const parts: Array<[number, "day" | "hour" | "minute"]> = [];
  if (days) parts.push([days, "day"]);
  if (hours) parts.push([hours, "hour"]);
  if (minutes) parts.push([minutes, "minute"]);
  return parts.slice(0, 2);
}

/** One unit of a wait as an ICU plural. @i18n-keys */
const WAIT_UNIT_LABEL = {
  day: "{count, plural, one {# day} other {# days}}",
  hour: "{count, plural, one {# hour} other {# hours}}",
  minute: "{count, plural, one {# minute} other {# minutes}}",
  second: "{count, plural, one {# second} other {# seconds}}",
} as const;

function Field({ label, children, testId, wide }: { label: string; children: React.ReactNode; testId: string; wide?: boolean }) {
  return (
    <div className={wide ? "flex min-w-0 flex-col gap-0.5 sm:col-span-2" : "flex min-w-0 flex-col gap-0.5"} data-testid={testId}>
      <dt className="content-compact text-default-secondary">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

export function GateDecisionRecord({
  record,
  title,
  byLabel,
  testId = "ctem-gate-record",
}: {
  record: CtemCaseGateRecord;
  /** The gate's own title when the record stands alone (the History timeline); a card already titles it. */
  title?: string;
  /** Who answered when it was not a person the directory names (the analyst's mark in OpenCTI). */
  byLabel?: string | null;
  testId?: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const when = (at: string | null) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : null);
  const status = record.status;
  const chip = gateChipOf(record);
  const answered = status === "answered";
  const passed = status === "passed_automatically";
  const reason = passed ? gateRecordReason(record.reason) : null;
  // Their directory name, else what the card knows (the analyst's mark), else "a person": never the raw id.
  const decidedBy = record.answered_by?.name || byLabel || t_i18n("a person");
  const recipient = (who: CtemCaseGateRecipient | null) =>
    who ? who.name || t_i18n(RECIPIENT_LABEL[who.type as keyof typeof RECIPIENT_LABEL] ?? RECIPIENT_LABEL.user) : null;
  const wait =
    record.wait_seconds != null
      ? waitParts(record.wait_seconds)
          .map(([value, unit]) => t_i18n(WAIT_UNIT_LABEL[unit], { count: value }))
          .join(" ")
      : null;
  const surface = record.surface ? t_i18n(GATE_SURFACE_LABEL[record.surface] ?? "XTM One") : null;
  const askedOf = recipient(record.asked_of);
  // The header time is the time of the status itself: answered, passed, or the deadline an expired gate ran out at.
  // A waiting gate shows its asked time and wait in the grid alone; a cancelled gate has no known time of its own.
  const statusAt = answered ? record.answered_at : passed ? record.passed_at : status === "expired" ? record.deadline : null;
  const next = record.next;
  const nextGate = next?.kind === "gate" && next.from ? next.from : null;

  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid={testId} data-status={status} data-chip={chip}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {title && <span className="font-medium">{title}</span>}
        <ToneBadge tone={GATE_CHIP_TONE[chip]} data-testid={`${testId}-status`}>
          {t_i18n(GATE_CHIP_LABEL[chip])}
        </ToneBadge>
        {statusAt && <span className="ml-auto whitespace-nowrap text-default-secondary">{when(statusAt)}</span>}
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        {answered && (
          <Field label={t_i18n("Decided by")} testId={`${testId}-by`}>
            {decidedBy}
          </Field>
        )}
        {answered && record.option && (
          <Field label={t_i18n("Option chosen")} testId={`${testId}-option`}>
            {record.option.label || t_i18n("Option {option}", { option: record.option.key })}
          </Field>
        )}
        {answered && record.comment && (
          <Field label={t_i18n("Comment")} testId={`${testId}-comment`} wide>
            <span className="whitespace-pre-line">{record.comment}</span>
          </Field>
        )}
        {reason && (
          <Field label={t_i18n("Why")} testId={`${testId}-reason`} wide>
            {t_i18n(reason.id, reason.values)}
          </Field>
        )}
        {!passed && askedOf && (
          <Field label={t_i18n("Asked of")} testId={`${testId}-asked-of`}>
            {askedOf}
          </Field>
        )}
        {record.asked_by_agent && (
          <Field label={t_i18n("Asked by")} testId={`${testId}-asked-by`}>
            <Link asChild>
              <RouterLink to={`/agents/${record.asked_by_agent.id}`}>{record.asked_by_agent.name || t_i18n("Agent")}</RouterLink>
            </Link>
          </Field>
        )}
        {!passed && record.asked_at && (
          <Field label={t_i18n("Asked at")} testId={`${testId}-asked-at`}>
            {when(record.asked_at)}
          </Field>
        )}
        {wait && (
          <Field label={status === "waiting" ? t_i18n("Waiting for") : t_i18n("Waited")} testId={`${testId}-wait`}>
            {wait}
          </Field>
        )}
        {status === "waiting" && record.deadline && (
          <Field label={t_i18n("Deadline")} testId={`${testId}-deadline`}>
            {when(record.deadline)}
          </Field>
        )}
        {answered && surface && (
          <Field label={t_i18n("Answered from")} testId={`${testId}-surface`}>
            {surface}
          </Field>
        )}
        {record.work_item_id && (
          <Field label={t_i18n("Decision")} testId={`${testId}-decision`}>
            <Link asChild>
              <RouterLink to={workItemPath(record.work_item_id)}>{t_i18n("Open the decision")}</RouterLink>
            </Link>
          </Field>
        )}
        {next && (
          <Field label={t_i18n("Next step")} testId={`${testId}-next`} wide>
            {/* The words the History timeline draws for the event, never its raw kind. */}
            <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              {nextGate ? (
                <span className="min-w-0">{t_i18n(GATE_LABEL[nextGate] ?? "Gate")}</span>
              ) : (
                <span className="min-w-0">{t_i18n((next.kind && CASE_EVENT_LABEL[next.kind]) || "Case updated")}</span>
              )}
              {next.kind === "state" && next.to && (
                <span>{t_i18n(CASE_STATE_LABEL[next.to as keyof typeof CASE_STATE_LABEL] ?? "Case updated")}</span>
              )}
              {nextGate && isGateOutcome(next.to) && <span className="font-medium">{t_i18n(GATE_OUTCOME_LABEL[next.to])}</span>}
              {next.headline && <span className="min-w-0 text-default-secondary">{next.headline}</span>}
              {next.actor_name && <span className="text-default-secondary">{t_i18n("by {name}", { name: next.actor_name })}</span>}
              <span className="whitespace-nowrap text-default-secondary">{when(next.at)}</span>
            </span>
          </Field>
        )}
      </dl>
      {answered && !record.recorded_on_case && (
        <p className="content-compact text-default-secondary" data-testid={`${testId}-pending`}>
          {t_i18n("Recorded on the case: pending")}
        </p>
      )}
    </div>
  );
}
