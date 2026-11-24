// SPDX-License-Identifier: Apache-2.0

/**
 * When a stage assessment was written, by whom, and whether it is out of date
 * (#12002). The stage page generates the user's assessment on visit when it is
 * missing or older than the weekly window, once per stage and vertical; the
 * backend refuses a second run while one is in flight.
 */
import { Icon } from "@filigran/design-system";
import { BadgeFocusTrigger } from "@/components/ui/badge";
import { ToneBadge } from "@/components/ui/tone-badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import type { CtemStageAssessmentResponse } from "@/types";

/**
 * Whether the page should start a generation now: nothing stored for the
 * vertical (pending), or a stored one past the weekly window, and nothing in
 * flight. A locked status (no license, AI off, no model) never generates.
 */
export function shouldAutoGenerate(resp: CtemStageAssessmentResponse | undefined): boolean {
  if (!resp || resp.generating || resp.status === "generating") return false;
  if (resp.status === "pending") return true;
  return resp.status === "ready" && !!resp.stale;
}

/**
 * The one byline of every AI briefing and assessment of the CTEM pages:
 * "By <agent> - <relative age>", the absolute date on hover. The exposure
 * briefing card, the exposure briefing page and the stage assessment all draw
 * it through this component (UX round 1 of #12012, B1).
 */
export function GeneratedByline({ agentName, generatedAt }: { agentName: string; generatedAt?: string | null }) {
  const { t_i18n, intl } = useFormatter();
  if (!generatedAt) return <>{t_i18n("By {name}", { name: agentName })}</>;
  const line = t_i18n("By {name} - {timeAgo}", { name: agentName, timeAgo: formatTimeAgo(intl, generatedAt) });
  const when = t_i18n("Generated {when}", { when: formatShortDateTimeIntl(intl, generatedAt) });
  return (
    <Tip content={when} side="top">
      <BadgeFocusTrigger aria-label={`${line}. ${when}`} data-testid="generated-byline">
        {line}
      </BadgeFocusTrigger>
    </Tip>
  );
}

/**
 * The stage assessment's byline, its Out of date marker past the window, and a
 * "Generating a new one..." marker while a run replaces the shown assessment,
 * so an old assessment never reads as current during the regeneration.
 */
export function AssessmentFreshness({
  generatedAt,
  agentName,
  stale,
  generating,
  freshDays = 7,
}: {
  generatedAt?: string | null;
  agentName?: string | null;
  stale?: boolean;
  /** A run is regenerating the assessment shown. */
  generating?: boolean;
  /** The freshness window in days (the backend's fresh_days). */
  freshDays?: number;
}) {
  const { t_i18n } = useFormatter();
  if (!generatedAt) return null;
  const staleTip = t_i18n("Older than {count, plural, one {# day} other {# days}}: a new one is generated on your next visit", {
    count: freshDays,
  });
  return (
    <div className="flex flex-wrap items-center gap-2 content-caption text-default-secondary" data-testid="assessment-freshness">
      <GeneratedByline agentName={agentName || t_i18n("AI assessment")} generatedAt={generatedAt} />
      {stale && (
        <Tip content={staleTip} side="top">
          <BadgeFocusTrigger aria-label={`${t_i18n("Out of date")}. ${staleTip}`}>
            <ToneBadge tone="orange" className="content-compact">
              {t_i18n("Out of date")}
            </ToneBadge>
          </BadgeFocusTrigger>
        </Tip>
      )}
      {generating && (
        <span className="inline-flex items-center gap-1 text-accent" role="status" data-testid="assessment-regenerating">
          <Icon name="sparkles" size={16} className="animate-pulse" aria-hidden />
          {t_i18n("Generating a new one...")}
        </span>
      )}
    </div>
  );
}
