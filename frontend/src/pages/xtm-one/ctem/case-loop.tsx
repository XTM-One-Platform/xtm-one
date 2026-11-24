// SPDX-License-Identifier: Apache-2.0

/**
 * The loop on a case (CTEM-03-UI, #892; dev-docs/ctem-loop-visibility.md):
 * its severity, the SLA countdown (P1 24 h, P2 72 h, P3 30 days, read from the
 * case's own `sla_due_at`), and the two marks the loop sets on it, reopened
 * (its latest validation failed) and escalated (its SLA was breached while
 * open). Shared by the Cases board, the case page and Act now, so the three
 * say the same thing in the same words.
 */
import { Chip, Icon } from "@filigran/design-system";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { type HotTone, ToneBadge } from "@/components/ui/tone-badge";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { CTEM_OPEN_CASE_STATES, isRestrictedIssue, type CtemCaseIssue, type CtemCasePriority, type CtemCaseSeverity, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { CasePriorityChip } from "./cases-shared";

type Translate = (id: string, values?: Record<string, string | number>) => string;

const OPEN_STATES: ReadonlySet<string> = new Set(CTEM_OPEN_CASE_STATES);

/** The loop severity in the hot tones of a priority (#1267): P1 red, P2 orange, P3 yellow. */
const SEVERITY_TONE: Record<CtemCaseSeverity, HotTone> = { P1: "red", P2: "orange", P3: "yellow" };

/** The countdown to the SLA: what is left, or how late, in the largest whole unit. */
export function slaCountdown(
  slaDueAt: string | null | undefined,
  now: number,
): { late: boolean; value: number; unit: "minute" | "hour" | "day" } | null {
  if (!slaDueAt) return null;
  const due = Date.parse(slaDueAt);
  if (Number.isNaN(due)) return null;
  const diff = due - now;
  const abs = Math.abs(diff);
  const minutes = Math.floor(abs / 60_000);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const late = diff < 0;
  if (days >= 2) return { late, value: days, unit: "day" };
  if (hours >= 1) return { late, value: hours, unit: "hour" };
  return { late, value: Math.max(minutes, 0), unit: "minute" };
}

/** The sentences of a countdown, by side of the SLA and unit. @i18n-keys */
const SLA_SENTENCES = {
  left: {
    minute: "{count, plural, one {{shown} min left} other {{shown} min left}}",
    hour: "{count, plural, one {{shown} h left} other {{shown} h left}}",
    day: "{count, plural, one {{shown} day left} other {{shown} days left}}",
  },
  late: {
    minute: "{count, plural, one {{shown} min past the SLA} other {{shown} min past the SLA}}",
    hour: "{count, plural, one {{shown} h past the SLA} other {{shown} h past the SLA}}",
    day: "{count, plural, one {{shown} day past the SLA} other {{shown} days past the SLA}}",
  },
} as const;

/** The sentence of a countdown. */
export function slaSentence(countdown: NonNullable<ReturnType<typeof slaCountdown>>, t_i18n: Translate, locale: string): string {
  const values = { count: countdown.value, shown: formatCount(locale, countdown.value) };
  return t_i18n(SLA_SENTENCES[countdown.late ? "late" : "left"][countdown.unit], values);
}

/**
 * The loop marks of a case: reopened by a failed re-test, escalated past its
 * SLA, closed on a passing re-test (CTEM-14-UI: resolved, and its latest
 * validation is a re-test that passed; `closedAt` and `closedProvider` say
 * when and where, never the run's internal reference: UX round 1 on #962, B3),
 * or closed at source (CTEM-25-UI: resolved, and its latest validation is a
 * passing `rescan`, the source closed every member, e.g. Aikido `issue.closed`).
 */
export function caseLoopMarks(c: Pick<CtemCaseSummary, "state" | "validations" | "sla_breached_at">): {
  reopened: boolean;
  escalated: boolean;
  closedOnRetest: boolean;
  closedAt: string | null;
  closedProvider: string | null;
  closedAtSource: boolean;
} {
  const open = OPEN_STATES.has(c.state);
  const last = c.validations?.length ? c.validations[c.validations.length - 1] : undefined;
  const passing = c.state === "resolved" && (last?.result === "passed" || last?.result === "not_exposed");
  const closedOnRetest = passing && last?.kind === "retest";
  const closedAtSource = passing && last?.kind === "rescan";
  const closed = closedOnRetest || closedAtSource;
  return {
    reopened: open && last?.result === "failed",
    escalated: open && Boolean(c.sla_breached_at),
    closedOnRetest,
    closedAt: closed && last ? last.at : null,
    closedProvider: closed && last ? (PROVIDER_NAMES[last.provider] ?? null) : null,
    closedAtSource,
  };
}

/** Product names of the validation providers a case can carry. */
const PROVIDER_NAMES: Readonly<Record<string, string>> = { openaev: "OpenAEV", opencti: "OpenCTI", aikido: "Aikido Security" };

/** The re-test words of an OpenAEV member of a case (CTEM-14-UI). @i18n-keys */
const RETEST_LABELS = {
  queued: "Re-test queued",
  notReproduced: "Not reproduced",
  resolved: "Resolved",
  reproduced: "Reproduced",
  notRetested: "Not re-tested yet",
} as const;

type RetestState = keyof typeof RETEST_LABELS;

/**
 * The colour of each re-test state (UX round 1 on #962, B4): still exploitable
 * reads high, waiting reads neutral or info, fixed reads low (the healthy tone).
 */
const RETEST_TONE: Record<RetestState, "high" | "info" | "neutral" | "low"> = {
  reproduced: "high",
  queued: "info",
  notRetested: "neutral",
  notReproduced: "low",
  resolved: "low",
};

/** The lifecycle order the column sorts by, in every language (advisory A2). */
const RETEST_RANK: Record<RetestState, number> = { queued: 0, reproduced: 1, notRetested: 2, notReproduced: 3, resolved: 4 };

/**
 * The re-test state of an OpenAEV member of a case (CTEM-14-UI): a queued
 * re-test first, then the finding's lifecycle status. Null for a member whose
 * source carries no lifecycle (OpenCTI, a signal, an OpenAEV older than the
 * lifecycle).
 */
function retestState(issue: CtemCaseIssue): RetestState | null {
  if (isRestrictedIssue(issue)) return null;
  if (issue.retest_pending) return "queued";
  if (issue.retest_status === "not_reproduced") return "notReproduced";
  if (issue.retest_status === "resolved") return "resolved";
  if (issue.retest_status === "open") return issue.last_tested_at ? "reproduced" : "notRetested";
  return null;
}

/** The sort rank of a member in the Re-test column; members with no lifecycle last. */
export function retestRank(issue: CtemCaseIssue): number {
  const state = retestState(issue);
  return state ? RETEST_RANK[state] : 99;
}

/** Whether a case's Re-test column has anything to show (UX round 1 on #962, B1). */
export function hasRetestData(issues: readonly CtemCaseIssue[]): boolean {
  return issues.some((issue) => retestState(issue) !== null);
}

/** The re-test chip of a member, nothing for a member with no lifecycle. */
export function RetestChip({ issue }: { issue: CtemCaseIssue }) {
  const { t_i18n } = useFormatter();
  const state = retestState(issue);
  if (!state) return null;
  return <Chip label={t_i18n(RETEST_LABELS[state])} severity={RETEST_TONE[state]} className="shrink-0" data-testid="ctem-case-issue-retest" />;
}

/** The sentence behind "Closed on passing re-test": where and when, in words. */
function closedSentence(provider: string | null, date: string | null, t_i18n: Translate): string {
  if (provider && date) return t_i18n("Re-test passed in {provider} on {date}", { provider, date });
  if (date) return t_i18n("Re-test passed on {date}", { date });
  return t_i18n("Closed on passing re-test");
}

/** The sentence behind "Closed at source": the source closed every member of the case. */
function closedAtSourceSentence(provider: string | null, date: string | null, t_i18n: Translate): string {
  if (provider && date) return t_i18n("{provider} closed every finding of this case on {date}", { provider, date });
  if (date) return t_i18n("The source closed every finding of this case on {date}", { date });
  return t_i18n("Closed at source");
}

function CaseSeverityChip({ severity }: { severity: CtemCaseSeverity | null | undefined }) {
  if (!severity) return null;
  return <ToneBadge tone={SEVERITY_TONE[severity]}>{severity}</ToneBadge>;
}

/**
 * The one urgency chip of a case (UX round 1 on #897, B3): the loop severity
 * when the case has one (the API then sets the priority from it), the priority
 * otherwise; never "P1" beside "Low".
 */
export function CaseUrgencyChip({ severity, priority }: { severity: CtemCaseSeverity | null | undefined; priority: CtemCasePriority }) {
  return severity ? <CaseSeverityChip severity={severity} /> : <CasePriorityChip priority={priority} />;
}

/**
 * The SLA line and the loop marks of an open case, one compact line. A case
 * with no SLA (no severity yet) and no mark draws nothing.
 */
export function CaseLoopLine({ c, now }: { c: CtemCaseSummary; now: number }) {
  const { t_i18n, intl } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const marks = caseLoopMarks(c);
  const countdown = OPEN_STATES.has(c.state) ? slaCountdown(c.sla_due_at, now) : null;
  if (!countdown && !marks.reopened && !marks.escalated && !marks.closedOnRetest && !marks.closedAtSource) return null;
  const closedDate = marks.closedAt ? intl.formatDate(marks.closedAt, { dateStyle: "medium" }) : null;
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 content-caption" data-testid="ctem-case-loop-line">
      {countdown && (
        <span className={countdown.late ? "text-feedback-error-primary" : "text-default-secondary"} data-testid="ctem-case-sla">
          {slaSentence(countdown, t_i18n, intl.locale)}
        </span>
      )}
      {marks.reopened && (
        <span className="flex items-center gap-1 text-default-primary" data-testid="ctem-case-reopened">
          <Icon name="refresh-cw" size={16} aria-hidden />
          {t_i18n("Reopened: re-test failed")}
        </span>
      )}
      {marks.escalated && (
        <span className="flex items-center gap-1 text-default-primary" data-testid="ctem-case-escalated">
          <Icon name="circle-alert" size={16} aria-hidden />
          {t_i18n("Escalated")}
        </span>
      )}
      {marks.closedOnRetest && (
        <Tip
          content={closedSentence(marks.closedProvider, closedDate, t_i18n)}
        >
          <span
            tabIndex={tabIndex}
            className={cn("flex items-center gap-1 text-default-primary", TIP_WRAP_FOCUS_RING)}
            data-testid="ctem-case-closed-on-retest"
          >
            <Icon name="circle-check" size={16} aria-hidden />
            {t_i18n("Closed on passing re-test")}
          </span>
        </Tip>
      )}
      {marks.closedAtSource && (
        <Tip content={closedAtSourceSentence(marks.closedProvider, closedDate, t_i18n)}>
          <span
            tabIndex={tabIndex}
            className={cn("flex items-center gap-1 text-default-primary", TIP_WRAP_FOCUS_RING)}
            data-testid="ctem-case-closed-at-source"
          >
            <Icon name="circle-check" size={16} aria-hidden />
            {t_i18n("Closed at source")}
          </span>
        </Tip>
      )}
    </span>
  );
}
