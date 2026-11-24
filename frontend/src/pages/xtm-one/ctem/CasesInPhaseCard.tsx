// SPDX-License-Identifier: Apache-2.0

/**
 * "Cases in this phase" on every CTEM stage page (#1621): the cases currently in
 * the page's phase, read once from `GET /ctem-cases/overview` (the list's
 * visibility, so every count equals the list it links to).
 *
 * One full-width card, its own row, so the AI assessment and Metrics row keeps
 * its equal heights. Counts by status, each a link to the list on that phase and
 * status; the decisions waiting, each a link to its case; the five cases that
 * need attention first (waiting first, then priority, then the oldest status)
 * with their priority, age, last change and the crown jewel they touch; a link
 * to the phase's list. Governance & Risk carries no case phase: the card counts
 * the decisions waiting and the risks accepted, across phases, under its own
 * title "Decisions and accepted risks" (#1624, UX B2).
 */
import { Link } from "react-router-dom";

import { Card, CardContent } from "@/components/ui/card";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  CTEM_CASE_STATES,
  useCtemCasesOverview,
  type CtemCasesOverviewCase,
  type CtemCasesOverviewWait,
  type CtemCaseState,
} from "@/hooks/api/useCtemCasesApi";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { CTEM_STAGE_CONFIG, type CtemStageKey } from "@/lib/constants";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

import { casesListHref } from "./cases-due";
import { CASE_STATE_LABEL, CasePriorityChip, CaseStateChip } from "./cases-shared";

const LINK = cn("content-compact font-medium text-accent hover:underline", BUTTON_ROLE_FOCUS_RING);

function CaseRow({ row, from, now }: { row: CtemCasesOverviewCase; from: { backTo: string; backLabel: string }; now: number }) {
  const { t_i18n, intl } = useFormatter();
  return (
    <li className="flex min-w-0 items-center gap-3 py-2" data-testid={`ctem-phase-case-${row.id}`}>
      <CasePriorityChip priority={row.priority} />
      <div className="min-w-0 flex-1">
        <Link to={`/ctem/cases/${row.id}`} state={from} className={cn(LINK, "block min-w-0")}>
          <TruncatedText text={row.title} />
        </Link>
        <p className="content-caption truncate text-default-secondary">
          {[
            row.created_at ? t_i18n("Opened {timeAgo}", { timeAgo: formatTimeAgo(intl, row.created_at, now) }) : null,
            row.updated_at ? t_i18n("changed {timeAgo}", { timeAgo: formatTimeAgo(intl, row.updated_at, now) }) : null,
            row.crown_jewel ? t_i18n("touches {asset}", { asset: row.crown_jewel }) : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      </div>
      <CaseStateChip state={row.state} />
    </li>
  );
}

function WaitRow({ wait, from, now }: { wait: CtemCasesOverviewWait; from: { backTo: string; backLabel: string }; now: number }) {
  const { t_i18n, intl } = useFormatter();
  return (
    <li className="flex min-w-0 items-center gap-2" data-testid={`ctem-phase-wait-${wait.case_id}`}>
      <Link to={`/ctem/cases/${wait.case_id}`} state={from} className={cn(LINK, "min-w-0 truncate")}>
        {wait.title}
      </Link>
      {wait.since && (
        <span className="content-caption shrink-0 text-default-secondary">
          {t_i18n("waiting since {timeAgo}", { timeAgo: formatTimeAgo(intl, wait.since, now) })}
        </span>
      )}
    </li>
  );
}

export function CasesInPhaseCard({ stage, backTo }: { stage: CtemStageKey | "governance"; backTo: string }) {
  const { t_i18n } = useFormatter();
  const now = useNow();
  const { data, isError } = useCtemCasesOverview();
  const stageLabel = t_i18n(stage === "governance" ? "Governance & Risk" : CTEM_STAGE_CONFIG[stage].label);
  const from = { backTo, backLabel: t_i18n("Back to {stage}", { stage: stageLabel }) };
  const title = stage === "governance" ? t_i18n("Decisions and accepted risks") : t_i18n("Cases in this phase");

  if (isError) {
    return (
      <Card data-testid="ctem-phase-cases">
        <CardContent className="space-y-1 p-5">
          <h2 className="content-base font-semibold">{title}</h2>
          <p className="content-compact text-default-secondary">{t_i18n("The cases could not be read. Try again in a moment.")}</p>
        </CardContent>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card data-testid="ctem-phase-cases" aria-busy>
        <CardContent className="space-y-2 p-5">
          <h2 className="content-base font-semibold">{title}</h2>
          <div className="h-5 w-48 animate-pulse rounded-sm bg-elevation-hover" />
        </CardContent>
      </Card>
    );
  }

  const governance = stage === "governance";
  const phase = governance ? null : data.phases[stage];
  const counts: Partial<Record<CtemCaseState, number>> = governance
    ? { waiting_decision: data.waiting.count, accepted_risk: data.counts.accepted_risk ?? 0 }
    : (phase?.counts ?? {});
  const waits = governance ? data.waiting.cases : data.waiting.cases.filter((w) => w.phase === stage);
  const top = phase?.top ?? [];
  const open = governance ? data.waiting.count + (data.counts.accepted_risk ?? 0) : (phase?.open ?? 0);
  const anyCase = data.open > 0 || CTEM_CASE_STATES.some((s) => (data.counts[s] ?? 0) > 0);
  const listHref = governance ? casesListHref({ state: "waiting_decision" }) : casesListHref({ phase: stage });
  const stateHref = (state: CtemCaseState) =>
    governance ? casesListHref({ state }) : `${casesListHref({ phase: stage })}&state=${state}`;

  const empty = !anyCase
    ? t_i18n("No case yet: cases appear when a signal matches your scope or open issues on your software share a fix.")
    : governance
      ? t_i18n("No decision waiting and no accepted risk.")
      : t_i18n("No open case in {stage}: every case is in another phase or resolved.", { stage: stageLabel });

  return (
    <Card data-testid="ctem-phase-cases">
      <CardContent className="space-y-3 p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="content-base font-semibold">{title}</h2>
          <Link to={listHref} className={LINK} data-testid="ctem-phase-cases-open">
            {governance ? t_i18n("Open the decisions waiting") : t_i18n("Open cases in {stage}", { stage: stageLabel })}
          </Link>
        </div>
        {open === 0 ? (
          <p className="content-compact text-default-secondary" data-testid="ctem-phase-cases-empty">
            {empty}
          </p>
        ) : (
          <>
            <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label={t_i18n("Cases by status")}>
              {CTEM_CASE_STATES.filter((s) => (counts[s] ?? 0) > 0).map((state) => (
                <li key={state}>
                  <Link to={stateHref(state)} className={LINK} data-testid={`ctem-phase-count-${state}`}>
                    {t_i18n("{state}: {count, plural, one {# case} other {# cases}}", {
                      state: t_i18n(CASE_STATE_LABEL[state]),
                      count: counts[state] ?? 0,
                    })}
                  </Link>
                </li>
              ))}
            </ul>
            {waits.length > 0 && (
              <div className="space-y-1" data-testid="ctem-phase-waits">
                <p className="content-caption font-medium text-default-secondary">
                  {t_i18n("{count, plural, one {# decision waiting} other {# decisions waiting}}", { count: waits.length })}
                </p>
                <ul className="space-y-1">
                  {waits.slice(0, 5).map((w) => (
                    <WaitRow key={w.case_id} wait={w} from={from} now={now} />
                  ))}
                </ul>
              </div>
            )}
            {top.length > 0 && (
              <div data-testid="ctem-phase-top">
                <p className="content-caption font-medium text-default-secondary">{t_i18n("Needs attention first")}</p>
                <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]">
                  {top.map((row) => (
                    <CaseRow key={row.id} row={row} from={from} now={now} />
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
