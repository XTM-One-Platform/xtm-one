// SPDX-License-Identifier: Apache-2.0

/**
 * The decision record of one CTEM gate (#1518, v2 #1679): the outcome chip with
 * its icon on top, then three steps, each a line of chips with icons: Asked
 * (the asking agent as an info chip, never the green link colour, who it was
 * asked of, when, the wait), Decided (the person, the option by its label, the
 * quoted comment, how long it waited, the channel) and Then (what ran next, the
 * new state as its chip), and two sentences: what the outcome means and what
 * happens next. Read from the resolved record `GET /ctem-cases/{id}` serves
 * (`gates`, `ctem_case_gate_records.resolve`). An answered decision always
 * reads as answered, whether or not the case recorded the answer yet. Used by
 * Analyst review, Validation and Remediation decision, and exported for the
 * History. Never prints a raw id or a raw option key: a person the viewer may
 * not see is "a person", a CTEM option key reads by its label.
 */
import type * as React from "react";
import { Link as RouterLink } from "react-router-dom";
import { Link, type IconName } from "@filigran/design-system";
import { ToneBadge, type HotTone } from "@/components/ui/tone-badge";
import type {
  CtemCaseEvent,
  CtemCaseGateRecipient,
  CtemCaseGateRecord,
  CtemCaseGateStatus,
  CtemCaseState,
} from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { workItemPath } from "@/lib/task-routes";
import { CASE_EVENT_LABEL } from "./case-events";
import { GATE_LABEL, GATE_OUTCOME_LABEL, GATE_REASON_LABEL, isGateOutcome, latestGate } from "./case-gates";
import { CASE_FACT_ICON, CaseFactChip, CaseIcon, actorIcon } from "./case-icons";
import { CASE_STATE_LABEL, CaseStateChip } from "./cases-shared";

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

const GATE_CHIP_ICON: Record<GateChip, IconName> = {
  waiting: "clock",
  answered: "circle-check",
  approved: "circle-check",
  rejected: "circle-x",
  deferred: "clock",
  passed_automatically: "zap",
  expired: "triangle-alert",
  cancelled: "ban",
};

/** What an outcome means, one sentence per chip. @i18n-keys */
const GATE_CHIP_MEANING = {
  waiting: "The case waits for this decision before the step runs.",
  answered: "A person answered: the option chosen sets what runs next.",
  approved: "A person approved: the step was allowed to run.",
  rejected: "A person refused: the step did not run as planned.",
  deferred: "A person put the step on hold.",
  passed_automatically: "No person was needed: the policy allowed the step.",
  expired: "Nobody answered before the deadline: the step did not run.",
  cancelled: "The decision was withdrawn before an answer.",
} as const satisfies Record<GateChip, string>;

/**
 * The CTEM gate option keys the agents write, by their label: a record whose
 * ask did not list its option (`option.label` null) never reads the raw key.
 * @i18n-keys
 */
const GATE_OPTION_LABEL = {
  approve: "Approve",
  reject: "Reject",
  safe_only: "Run the read-only checks only",
  retry_in_draft: "Rework the package as a draft",
  hold: "Hold",
  not_approved: "Not approved",
} as const;

const OPTION_LABEL: Readonly<Record<string, string>> = GATE_OPTION_LABEL;

/** The first word of an option key or label that tells its meaning. */
const ANSWER_MEANING: ReadonlyArray<[RegExp, "approved" | "rejected" | "deferred"]> = [
  [/^(approve[ds]?|approval|accept(ed|s)?|allow(ed|s)?|yes|go|confirm(ed|s)?)$/, "approved"],
  [/^(reject(ed|s)?|decline[ds]?|deny|denied|refuse[ds]?|no|block(ed|s)?)$/, "rejected"],
  [/^(defer(red|s)?|postpone[ds]?|later|snooze[ds]?|wait|hold)$/, "deferred"],
];

/** Whole option keys whose first word does not tell their meaning. */
const KEY_MEANING: Readonly<Record<string, "approved" | "rejected" | "deferred">> = { not_approved: "rejected" };

function meaningOf(text: string | null | undefined): "approved" | "rejected" | "deferred" | null {
  const whole = (text ?? "").trim().toLowerCase();
  if (KEY_MEANING[whole]) return KEY_MEANING[whole];
  const word = whole.split(/[^a-z]+/)[0] ?? "";
  return ANSWER_MEANING.find(([pattern]) => pattern.test(word))?.[1] ?? null;
}

/** The chip of a record: an answer by the meaning of its option key, else its label, else "answered". */
export function gateChipOf(record: Pick<CtemCaseGateRecord, "status" | "option">): GateChip {
  if (record.status !== "answered") return record.status;
  return meaningOf(record.option?.key) ?? meaningOf(record.option?.label) ?? "answered";
}

/** The catalog id of an option: its own label, else the known CTEM key, else null (drawn "Option {option}"). */
export function gateOptionLabel(option: CtemCaseGateRecord["option"]): string | null {
  if (!option) return null;
  return option.label || OPTION_LABEL[option.key.toLowerCase()] || null;
}

/**
 * Who answered a gate when the record cannot name them (the decision row is
 * unreadable to the viewer, or the answer came as free text): the page
 * directory's name of the answering id, else the actor name the case History
 * draws on the `gate answered` event, else null ("a person").
 */
export function gateAnswererName(
  record: Pick<CtemCaseGateRecord, "key" | "answered_by">,
  history: readonly CtemCaseEvent[] | undefined,
  personName?: (id: string) => string | null,
): string | null {
  if (record.answered_by?.name) return record.answered_by.name;
  const byId = record.answered_by ? personName?.(record.answered_by.id) : null;
  if (byId) return byId;
  const event = history ? latestGate(history, record.key) : null;
  if (event?.to !== "answered") return null;
  return (event.reason && personName?.(event.reason)) || event.actor_name || null;
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

/** What happens next when no later event is recorded yet, by chip; null when the Then step says it. @i18n-keys */
const GATE_CHIP_NEXT = {
  waiting: "Answer the decision to let the case go on.",
  expired: "Nothing ran from this decision.",
  cancelled: "Nothing ran from this decision.",
} as const satisfies Partial<Record<GateChip, string>>;

/** One fact of a step: its icon and label above, its value (a chip or a line) below. */
function Field({
  label,
  icon,
  children,
  testId,
  wide,
}: {
  label: string;
  icon: IconName;
  children: React.ReactNode;
  testId: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "flex min-w-0 flex-col gap-1 sm:col-span-2" : "flex min-w-0 flex-col gap-1"} data-testid={testId}>
      <dt className="flex items-center gap-1.5 content-compact text-default-secondary">
        <CaseIcon name={icon} />
        {label}
      </dt>
      <dd className="flex min-w-0 flex-wrap items-center gap-1.5 break-words">{children}</dd>
    </div>
  );
}

/** A step of the record (Asked, Decided, Then): its heading and its facts. */
function Step({ label, icon, testId, children }: { label: string; icon: IconName; testId: string; children: React.ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-2 border-l-2 border-elevation-subtle pl-3" data-testid={testId} aria-label={label}>
      <h4 className="flex items-center gap-1.5 font-medium">
        <CaseIcon name={icon} className="text-default-secondary" />
        {label}
      </h4>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">{children}</dl>
    </section>
  );
}

export function GateDecisionRecord({
  record,
  title,
  byLabel,
  testId = "ctem-gate-record",
}: {
  record: CtemCaseGateRecord;
  /** The gate's own title when the record stands alone (the History dialog); a card already titles it. */
  title?: string;
  /** Who answered when the record does not name them (gateAnswererName, or the analyst's mark in OpenCTI). */
  byLabel?: string | null;
  testId?: string;
}) {
  const { t_i18n, t_text, intl } = useFormatter();
  const when = (at: string | null) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : null);
  const status = record.status;
  const chip = gateChipOf(record);
  const answered = status === "answered";
  const passed = status === "passed_automatically";
  const reason = passed ? gateRecordReason(record.reason) : null;
  // Their directory name, else what the card knows (the History actor, the analyst's mark), else "a person": never the raw id.
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
  const optionLabel = gateOptionLabel(record.option);
  // The header time is the time of the status itself: answered, passed, or the deadline an expired gate ran out at.
  // A waiting gate shows its asked time and wait in the Asked step alone; a cancelled gate has no known time of its own.
  const statusAt = answered ? record.answered_at : passed ? record.passed_at : status === "expired" ? record.deadline : null;
  const next = record.next;
  const nextGate = next?.kind === "gate" && next.from ? next.from : null;
  const nextState = next?.kind === "state" && next.to && next.to in CASE_STATE_LABEL ? (next.to as CtemCaseState) : null;
  const waiting = status === "waiting";
  // A waiting gate opens its decision from the Asked step: nobody has decided it yet (UX round 4 on #1708, B8).
  const showAsked = !passed && (!!record.asked_by_agent || !!askedOf || !!record.asked_at || (waiting && !!record.work_item_id));
  const decisionLink = record.work_item_id ? (
    <Field label={t_i18n("Decision")} icon={CASE_FACT_ICON.decision} testId={`${testId}-decision`}>
      <Link asChild>
        <RouterLink to={workItemPath(record.work_item_id)}>{t_i18n("Open the decision")}</RouterLink>
      </Link>
    </Field>
  ) : null;
  // A waiting gate with no decision filed has nothing to answer (#1954): the card's own line says what resumes it.
  const nextSentence =
    waiting && record.decision_filed === false ? null : (GATE_CHIP_NEXT as Partial<Record<GateChip, string>>)[chip];
  // The second step is named for what happened: nobody decided an expired or a cancelled gate (UX round 3 on #1708, B7).
  const outcomeStep = passed
    ? { label: t_i18n("Passed"), icon: CASE_FACT_ICON.decided }
    : status === "expired"
      ? { label: t_i18n("No answer"), icon: CASE_FACT_ICON.wait }
      : status === "cancelled"
        ? { label: t_i18n("Cancelled"), icon: CASE_FACT_ICON.state }
        : // Its own key (advisory A1 of #1708 round 4): the shared "Decided" reads as a date label in ja.
          { label: t_i18n("Decided (gate step)"), icon: CASE_FACT_ICON.decided };

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid={testId} data-status={status} data-chip={chip}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {title && <span className="font-medium">{title}</span>}
        <ToneBadge tone={GATE_CHIP_TONE[chip]} icon={<CaseIcon name={GATE_CHIP_ICON[chip]} />} data-testid={`${testId}-status`}>
          {t_i18n(GATE_CHIP_LABEL[chip])}
        </ToneBadge>
        {statusAt && <span className="ml-auto whitespace-nowrap text-default-secondary">{when(statusAt)}</span>}
      </div>
      {showAsked && (
        <Step label={t_i18n("Asked (gate step)")} icon={CASE_FACT_ICON.asked} testId={`${testId}-step-asked`}>
          {record.asked_by_agent && (
            <Field label={t_i18n("Asked by")} icon={CASE_FACT_ICON.agent} testId={`${testId}-asked-by`}>
              <CaseFactChip icon={CASE_FACT_ICON.agent} tone="blue" to={`/agents/${record.asked_by_agent.id}`}>
                {record.asked_by_agent.name || t_i18n("Agent")}
              </CaseFactChip>
            </Field>
          )}
          {askedOf && (
            <Field label={t_i18n("Asked of")} icon={actorIcon(record.asked_of?.type)} testId={`${testId}-asked-of`}>
              <CaseFactChip icon={actorIcon(record.asked_of?.type)}>{askedOf}</CaseFactChip>
            </Field>
          )}
          {record.asked_at && (
            <Field label={t_i18n("Asked at")} icon={CASE_FACT_ICON.deadline} testId={`${testId}-asked-at`}>
              {when(record.asked_at)}
            </Field>
          )}
          {status === "waiting" && wait && (
            <Field label={t_i18n("Waiting for")} icon={CASE_FACT_ICON.wait} testId={`${testId}-wait`}>
              <CaseFactChip icon={CASE_FACT_ICON.wait} tone="yellow">{wait}</CaseFactChip>
            </Field>
          )}
          {status === "waiting" && record.deadline && (
            <Field label={t_i18n("Deadline")} icon={CASE_FACT_ICON.deadline} testId={`${testId}-deadline`}>
              {when(record.deadline)}
            </Field>
          )}
          {waiting && decisionLink}
        </Step>
      )}
      {!waiting && (answered || reason || wait || record.work_item_id) && (
        <Step label={outcomeStep.label} icon={outcomeStep.icon} testId={`${testId}-step-decided`}>
          {answered && (
            <Field label={t_i18n("Decided by")} icon={CASE_FACT_ICON.person} testId={`${testId}-by`}>
              <CaseFactChip icon={CASE_FACT_ICON.person}>{decidedBy}</CaseFactChip>
            </Field>
          )}
          {answered && record.option && (
            <Field label={t_i18n("Option chosen")} icon={GATE_CHIP_ICON[chip]} testId={`${testId}-option`}>
              <ToneBadge tone={GATE_CHIP_TONE[chip]} className="max-w-full">
                <span className="min-w-0 truncate">{optionLabel ? t_text(optionLabel) : t_i18n("Option {option}", { option: record.option.key })}</span>
              </ToneBadge>
            </Field>
          )}
          {reason && (
            <Field label={t_i18n("Why")} icon={CASE_FACT_ICON.meaning} testId={`${testId}-reason`} wide>
              {t_i18n(reason.id, reason.values)}
            </Field>
          )}
          {wait && (
            <Field label={t_i18n("Waited")} icon={CASE_FACT_ICON.wait} testId={`${testId}-wait`}>
              <CaseFactChip icon={CASE_FACT_ICON.wait}>{wait}</CaseFactChip>
            </Field>
          )}
          {answered && surface && (
            <Field label={t_i18n("Answered from")} icon={CASE_FACT_ICON.channel} testId={`${testId}-surface`}>
              <CaseFactChip icon={CASE_FACT_ICON.channel}>{surface}</CaseFactChip>
            </Field>
          )}
          {answered && record.comment && (
            <Field label={t_i18n("Comment")} icon={CASE_FACT_ICON.comment} testId={`${testId}-comment`} wide>
              <blockquote className="whitespace-pre-line border-l-2 border-elevation-subtle pl-2 italic">{record.comment}</blockquote>
            </Field>
          )}
          {decisionLink}
        </Step>
      )}
      {next && (
        <Step label={t_i18n("Then")} icon={CASE_FACT_ICON.then} testId={`${testId}-step-then`}>
          <Field label={t_i18n("Next step")} icon={CASE_FACT_ICON.next} testId={`${testId}-next`} wide>
            {/* The words the History draws for the event, never its raw kind. */}
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              {nextGate ? (
                <span className="min-w-0">{t_i18n(GATE_LABEL[nextGate] ?? "Gate")}</span>
              ) : (
                <span className="min-w-0">{t_i18n((next.kind && CASE_EVENT_LABEL[next.kind]) || "Case updated")}</span>
              )}
              {nextState && <CaseStateChip state={nextState} />}
              {next.kind === "state" && next.to && !nextState && <span>{t_i18n("Case updated")}</span>}
              {nextGate && isGateOutcome(next.to) && <span className="font-medium">{t_i18n(GATE_OUTCOME_LABEL[next.to])}</span>}
              {next.headline && <span className="min-w-0 text-default-secondary">{next.headline}</span>}
              {next.actor_name && (
                <CaseFactChip icon={actorIcon(next.actor_type)} tone={next.actor_type === "agent" ? "blue" : "neutral"}>
                  {next.actor_name}
                </CaseFactChip>
              )}
              <span className="whitespace-nowrap text-default-secondary">{when(next.at)}</span>
            </span>
          </Field>
        </Step>
      )}
      {/* Two columns only for two facts: one fact alone spans the body (UX round 1 on #1708, B2). */}
      <dl className={nextSentence ? "grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2" : "grid grid-cols-1 gap-y-2"} data-testid={`${testId}-explain`}>
        <Field label={t_i18n("What it means")} icon={CASE_FACT_ICON.meaning} testId={`${testId}-meaning`}>
          {t_i18n(GATE_CHIP_MEANING[chip])}
        </Field>
        {nextSentence && (
          <Field label={t_i18n("What happens next")} icon={CASE_FACT_ICON.next} testId={`${testId}-what-next`}>
            {t_i18n(nextSentence)}
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
