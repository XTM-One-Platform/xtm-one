// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM Cases row of the command center (dev-docs/ctem-cases.md section
 * 7.4, UX decision Q1, #11545 slice S5a): one compact full-width row with the
 * four state Chips and their exact counts over one segmented bar in the state
 * colours of 7.0, then "N overdue" and "N accepted risks expire in 14 days",
 * each a link to the filtered Cases list. The counts are the list's own
 * (`GET /ctem-cases`, computed on the cases the caller sees, #11613), so the
 * row never counts a case the list would hide. No case and nothing to build
 * them from (no software inventory, no OpenAEV): one dismissible setup row.
 *
 * Cases in motion (#1621): each segment of the bar says its count and share on
 * hover, and the row ends with "Waiting for a decision: N, oldest X", a link
 * to the case that waits longest (`GET /ctem-cases/overview`), absent at 0. No
 * inline approval: decisions stay in the unified decision system.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { CTEM_CASE_STATES, useCtemCases, useCtemCasesOverview, type CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";

import { CASE_STATE_LABEL, CaseStateChip } from "./cases-shared";
import { caseDueFlags, casesListHref, EXPIRING_DAYS } from "./cases-due";
import { profileFieldHref } from "@/lib/organization-profile-tabs";

/**
 * The segment fill of each state: the feedback token of its hot tone
 * (7.0, #1267), the same colour as its badge. Accepted risk takes the neutral token, never the track's colour, so
 * its segment stays visible (at least 3:1 on the track, both themes).
 */
const SEGMENT_FILL: Record<CtemCaseState, string> = {
  new: "bg-feedback-warning-primary",
  analyzing: "bg-feedback-info-primary",
  waiting_decision: "bg-feedback-error-primary",
  remediating: "bg-feedback-info-primary",
  validating: "bg-feedback-info-primary",
  mitigated_watching: "bg-feedback-alert-primary",
  resolved: "bg-feedback-success-primary",
  accepted_risk: "bg-feedback-neutral-primary",
};

const LINK = cn("content-compact font-medium text-accent hover:underline", BUTTON_ROLE_FOCUS_RING);

export interface CasesCountsCardProps {
  /** Whether cases can be built at all: a software inventory or OpenAEV connected. */
  configured: boolean;
  /** Open the OpenAEV connect dialog of the page. */
  onConnectOpenAev: () => void;
}

export function CasesCountsCard({ configured, onConnectOpenAev }: CasesCountsCardProps) {
  const { t_i18n, intl } = useFormatter();
  const now = useNow();
  // One section of the command center: a failed read stays in this row.
  const { data, isLoading, isError, refetch } = useCtemCases({ throwOnError: false });
  const { data: overview } = useCtemCasesOverview();
  const [setupClosed, setSetupClosed] = useState(false);
  const counts = data?.counts;
  const total = counts ? CTEM_CASE_STATES.reduce((sum, s) => sum + (counts[s] ?? 0), 0) : null;
  const due = useMemo(() => {
    let overdue = 0;
    let expiring = 0;
    for (const c of data?.cases ?? []) {
      const flags = caseDueFlags(c, now);
      if (flags.includes("overdue")) overdue += 1;
      if (flags.includes("expiring")) expiring += 1;
    }
    return { overdue, expiring };
  }, [data, now]);
  const waiting = overview?.waiting;
  const waitingAge = (hours: number | null | undefined) =>
    hours == null
      ? null
      : hours < 48
        ? t_i18n("{hours} h", { hours: Math.max(1, Math.round(hours)) })
        : t_i18n("{days} d", { days: Math.round(hours / 24) });

  const header = (
    <div className="flex items-center justify-between gap-2">
      <h3 className="content-base font-semibold">{t_i18n("Cases")}</h3>
      <Link to="/ctem/cases" className={LINK}>
        {t_i18n("Open Cases")}
      </Link>
    </div>
  );

  let body;
  if (isError && !data) {
    body = (
      <p className="content-compact text-default-secondary">
        {t_i18n("Cases could not be read.")}{" "}
        <button type="button" onClick={() => void refetch()} className={LINK}>
          {t_i18n("Retry")}
        </button>
      </p>
    );
  } else if (total === 0 && !configured) {
    if (setupClosed) return null;
    return (
      <Card className="flex h-full flex-col overflow-hidden" data-testid="ctem-cases-card">
        <CardContent className="flex flex-1 flex-wrap items-center gap-3 p-4">
          <p className="min-w-0 flex-1 content-compact">{t_i18n("Build cases from your software")}</p>
          <Button asChild variant="outline">
            <Link to={profileFieldHref("tech_stack")}>{t_i18n("Add software")}</Link>
          </Button>
          <Button variant="outline" onClick={onConnectOpenAev}>
            {t_i18n("Connect OpenAEV")}
          </Button>
          <Button variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={() => setSetupClosed(true)}>
            <Icon name="x" size={16} />
          </Button>
        </CardContent>
      </Card>
    );
  } else if (total === 0) {
    body = (
      <p className="content-compact text-default-secondary">
        {t_i18n("No case yet: cases appear when open issues on your software share a fix.")}
      </p>
    );
  } else {
    body = (
      <div className="flex flex-col gap-2" aria-busy={isLoading && !data}>
        {/* The chips carry every count for a screen reader; the segments add the share on hover. */}
        <div className="flex h-2 overflow-hidden rounded-full bg-elevation-hover" aria-hidden="true" data-testid="ctem-cases-bar">
          {total
            ? CTEM_CASE_STATES.map((state) => {
                const n = counts?.[state] ?? 0;
                if (n <= 0) return null;
                const share = Math.round((n / total) * 100);
                return (
                  <Tip
                    key={state}
                    content={t_i18n("{state}: {count, plural, one {# case} other {# cases}}, {share}%", {
                      state: t_i18n(CASE_STATE_LABEL[state]),
                      count: n,
                      share,
                    })}
                  >
                    <span
                      className={cn("h-full", SEGMENT_FILL[state])}
                      style={{ width: `${(n / total) * 100}%` }}
                      data-testid={`ctem-cases-segment-${state}`}
                    />
                  </Tip>
                );
              })
            : null}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <ul className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {CTEM_CASE_STATES.map((state) => {
              // A state the API leaves out holds no case: zero, never a dash.
              const count = counts ? (counts[state] ?? 0) : undefined;
              const label = t_i18n(CASE_STATE_LABEL[state]);
              return (
                <li key={state}>
                  <Link
                    to={casesListHref({ state })}
                    aria-label={count === undefined ? label : t_i18n("{state}: {count, plural, one {# case} other {# cases}}", { count, state: label })}
                    // A block-level flex row: an inline-flex link would sit on the <li>'s text baseline, which
                    // follows its first item, so a chip with a leading icon rose above its siblings.
                    className={cn("flex items-center gap-1.5 rounded-sm", BUTTON_ROLE_FOCUS_RING)}
                    data-testid={`ctem-cases-count-${state}`}
                  >
                    <CaseStateChip state={state} />
                    <span className="content-compact font-semibold tabular-nums">
                      {count === undefined ? "-" : formatCount(intl.locale, count)}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          {due.overdue > 0 && (
            <Link to={casesListHref({ due: "overdue" })} className={LINK} data-testid="ctem-cases-overdue">
              {t_i18n("{count, plural, one {# overdue} other {# overdue}}", { count: due.overdue })}
            </Link>
          )}
          {due.expiring > 0 && (
            <Link to={casesListHref({ due: "expiring" })} className={LINK} data-testid="ctem-cases-expiring">
              {t_i18n("{count, plural, one {# accepted risk expires in {days} days} other {# accepted risks expire in {days} days}}", {
                count: due.expiring,
                days: EXPIRING_DAYS,
              })}
            </Link>
          )}
          {waiting && waiting.count > 0 && waiting.oldest && (
            <Link
              to={`/ctem/cases/${waiting.oldest.case_id}`}
              state={{ backTo: "/", backLabel: "Back to Command Center" }}
              className={cn(LINK, "ml-auto")}
              data-testid="ctem-cases-waiting"
            >
              {(() => {
                const age = waitingAge(waiting.oldest_hours);
                return age
                  ? t_i18n("Waiting for a decision: {count}, oldest {age}", { count: waiting.count, age })
                  : t_i18n("Waiting for a decision: {count}", { count: waiting.count });
              })()}
            </Link>
          )}
        </div>
      </div>
    );
  }

  return (
    <Card className="flex h-full flex-col overflow-hidden" data-testid="ctem-cases-card">
      <CardContent className="flex flex-1 flex-col gap-2 p-4">
        {header}
        <div className="min-h-0 flex-1">{body}</div>
      </CardContent>
    </Card>
  );
}
