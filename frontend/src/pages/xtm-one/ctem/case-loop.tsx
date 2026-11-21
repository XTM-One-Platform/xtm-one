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
import type { CtemCaseSeverity, CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";

type Translate = (id: string, values?: Record<string, string | number>) => string;

const OPEN_STATES: ReadonlySet<string> = new Set(["needs_attention", "in_progress"]);

const SEVERITY_CHIP: Record<CtemCaseSeverity, "critical" | "high" | "medium"> = { P1: "critical", P2: "high", P3: "medium" };

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

/** The sentence of a countdown. @i18n-keys */
export function slaSentence(countdown: NonNullable<ReturnType<typeof slaCountdown>>, t_i18n: Translate, locale: string): string {
  const values = { count: countdown.value, shown: formatCount(locale, countdown.value) };
  const sentences = {
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
  return t_i18n(sentences[countdown.late ? "late" : "left"][countdown.unit], values);
}

/** The loop marks of a case: reopened by a failed re-test, escalated past its SLA. */
export function caseLoopMarks(c: Pick<CtemCaseSummary, "state" | "validations" | "sla_breached_at">): {
  reopened: boolean;
  escalated: boolean;
} {
  const open = OPEN_STATES.has(c.state);
  const last = c.validations?.length ? c.validations[c.validations.length - 1] : undefined;
  return { reopened: open && last?.result === "failed", escalated: open && Boolean(c.sla_breached_at) };
}

export function CaseSeverityChip({ severity }: { severity: CtemCaseSeverity | null | undefined }) {
  if (!severity) return null;
  return <Chip label={severity} severity={SEVERITY_CHIP[severity]} className="shrink-0" />;
}

/**
 * The SLA line and the loop marks of an open case, one compact line. A case
 * with no SLA (no severity yet) and no mark draws nothing.
 */
export function CaseLoopLine({ c, now }: { c: CtemCaseSummary; now: number }) {
  const { t_i18n, intl } = useFormatter();
  const marks = caseLoopMarks(c);
  const countdown = OPEN_STATES.has(c.state) ? slaCountdown(c.sla_due_at, now) : null;
  if (!countdown && !marks.reopened && !marks.escalated) return null;
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
    </span>
  );
}
