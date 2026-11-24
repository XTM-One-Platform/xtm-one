// SPDX-License-Identifier: Apache-2.0

/**
 * The Cases board, signature viz of the Mobilization stage page
 * (dev-docs/ctem-cases.md section 7.1, S5c of #11545): a flow strip of the
 * eight case statuses (#1503), never a Kanban (drag would bypass the owner,
 * evidence and expiry rules). Each open column carries its exact count, the ageing bar (one blue
 * ramp, section 7.0), the overdue line and its three highest-priority cases.
 * Drawn only from the cases the caller sees (#11613); nothing is sampled.
 * CTEM-03-UI (#892): each top case shows its severity, the SLA countdown and
 * the reopened or escalated mark; the `ctem_case_update` frame re-reads the
 * list, so a card moves column live, and the countdown ticks every minute.
 */
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Skeleton } from "@/components/ui/skeleton";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import { useNow } from "@/hooks/useTick";
import { CTEM_CASE_STATES, isOpenCaseState, useCtemCases, type CtemCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { exactCount, formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { BOARD_COLUMN_SURFACE, BOARD_GRID, DAY, OVERDUE_TEXT, ageBands, daysSince, topCases } from "./cases-board";
import { CaseLoopLine, CaseUrgencyChip } from "./case-loop";
import { CaseTicketChip } from "./case-ticket";
import { CASE_STATE_LABEL, CaseOwner, CaseStateChip, useCasesEmptyLabel } from "./cases-shared";

const PERIODS = ["7", "30", "90"] as const;
type Period = (typeof PERIODS)[number];

/**
 * Age bands of an open case (time in its state), older reads stronger in both
 * themes. Tokens and ratios measured on the column surface: section 7.0.
 */
const AGE_BANDS = [
  { key: "0-7", label: "0-7 d", className: "bg-[var(--blue-700)] light:bg-[var(--blue-600)]" },
  { key: "8-30", label: "8-30 d", className: "bg-[var(--blue-600)] light:bg-[var(--blue-700)]" },
  { key: "31-90", label: "31-90 d", className: "bg-[var(--blue-400)] light:bg-[var(--blue-800)]" },
  { key: "90+", label: "> 90 d", className: "bg-[var(--blue-200)] light:bg-[var(--darkblue-700)]" },
] as const;

function AgeingBar({ cases, now }: { cases: CtemCaseSummary[]; now: number }) {
  const { t_i18n, intl } = useFormatter();
  const counts = ageBands(cases, now);
  const total = counts.reduce((s, n) => s + n, 0);
  if (total === 0) return null;
  return (
    <div className="space-y-1.5">
      <div
        className="flex h-2 w-full gap-0.5 overflow-hidden rounded-sm"
        role="img"
        aria-label={AGE_BANDS.map((b, i) => `${t_i18n(b.label)}: ${exactCount(intl.locale, counts[i])}`).join(", ")}
      >
        {AGE_BANDS.map((b, i) =>
          counts[i] > 0 ? <span key={b.key} className={cn("h-full", b.className)} style={{ flexGrow: counts[i] }} /> : null,
        )}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 content-caption text-default-secondary">
        {AGE_BANDS.map((b, i) => (
          <li key={b.key} className="flex items-center gap-1">
            <span className={cn("inline-block size-2 rounded-sm", b.className)} aria-hidden />
            {t_i18n(b.label)} {exactCount(intl.locale, counts[i])}
          </li>
        ))}
      </ul>
    </div>
  );
}

function TopCase({ c, owner, now }: { c: CtemCaseSummary; owner: string | null; now: number }) {
  const { t_i18n, intl } = useFormatter();
  const age = daysSince(c.state_since, now);
  return (
    <li>
      <Link
        to={`/ctem/cases/${c.id}`}
        className="flex min-w-0 flex-col gap-1 rounded-sm p-2 hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        data-testid="ctem-cases-board-top"
      >
        <TruncatedText text={c.title} className="content-base font-medium" />
        <span className="flex min-w-0 items-center gap-2 content-caption text-default-secondary">
          <CaseUrgencyChip severity={c.severity} priority={c.priority} />
          <span className="whitespace-nowrap">
            {t_i18n("{count, plural, one {# issue} other {# issues}}", { count: c.open_issue_count })}
          </span>
          {age !== null && <span className="whitespace-nowrap">{t_i18n("{days} d", { days: formatCount(intl.locale, age) })}</span>}
          <span className="ml-auto min-w-0 max-w-[9rem]">
            <CaseOwner name={owner} />
          </span>
        </span>
        <CaseLoopLine c={c} now={now} />
        <CaseTicketChip tickets={c.tickets} compact />
      </Link>
    </li>
  );
}

/** The one-line inflow head of a column: cases now in this state that entered it in the period. */
function inflowLine(state: CtemCaseState, n: number, period: number, t: (k: string, v?: Record<string, string | number>) => string) {
  if (n === 0) return t("No case entered in {days} d", { days: period });
  const lines: Record<CtemCaseState, string> = {
    new: "{count, plural, one {+# new or reopened in {days} d} other {+# new or reopened in {days} d}}",
    analyzing: "{count, plural, one {+# analysis started in {days} d} other {+# analyses started in {days} d}}",
    waiting_decision: "{count, plural, one {+# awaiting a decision in {days} d} other {+# awaiting a decision in {days} d}}",
    remediating: "{count, plural, one {+# remediation started in {days} d} other {+# remediations started in {days} d}}",
    validating: "{count, plural, one {+# validation started in {days} d} other {+# validations started in {days} d}}",
    mitigated_watching: "{count, plural, one {+# mitigated in {days} d} other {+# mitigated in {days} d}}",
    resolved: "{count, plural, one {+# resolved in {days} d} other {+# resolved in {days} d}}",
    accepted_risk: "{count, plural, one {+# risk accepted in {days} d} other {+# risk accepted in {days} d}}",
  };
  return t(lines[state], { count: n, days: period });
}

export function CasesBoard() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  // A section of the stage page: a failed read keeps its own Retry (UX r1 B4 on #11666).
  const { data, isLoading, isError, refetch } = useCtemCases({ throwOnError: false });
  const [periodKey, setPeriodKey] = useState<Period>("30");
  const period = Number(periodKey);
  // The SLA countdown moves without a reload (CTEM-03-UI, #892).
  const now = useNow(60_000);

  const cases = useMemo(() => data?.cases ?? [], [data]);
  // A product that could not be read never yields the all-clear line (#12139).
  const empty = useCasesEmptyLabel(data?.details_unavailable);
  const byState = useMemo(() => {
    const out = Object.fromEntries(CTEM_CASE_STATES.map((s) => [s, [] as CtemCaseSummary[]])) as Record<CtemCaseState, CtemCaseSummary[]>;
    for (const c of cases) out[c.state]?.push(c);
    return out;
  }, [cases]);
  const ownerIds = useMemo(
    () => [...new Set(cases.map((c) => c.owner_user_id).filter((id): id is string => Boolean(id)))],
    [cases],
  );
  const { data: owners } = useUserDirectoryByIds(ownerIds);
  const ownerName = (id: string | null) => (id ? (owners?.find((o) => o.id === id)?.full_name ?? null) : null);

  const openCases = cases.filter((c) => isOpenCaseState(c.state));
  const openIssues = openCases.reduce((s, c) => s + c.open_issue_count, 0);
  const enteredIn = (list: CtemCaseSummary[]) =>
    list.filter((c) => {
      const d = daysSince(c.state_since, now);
      return d !== null && d <= period;
    });

  if (isLoading && !data) {
    return (
      <Card>
        <CardContent className="space-y-4 p-5">
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-56 w-full" />
        </CardContent>
      </Card>
    );
  }
  if (isError && !data) {
    return (
      <Card>
        <CardContent className="flex items-center justify-between gap-4 p-5">
          <span className="content-base">{t_i18n("Cases could not be read.")}</span>
          <Button variant="outline" size="sm" onClick={() => void refetch()}>
            {t_i18n("Retry")}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="ctem-cases-board">
      <CardContent className="space-y-4 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h2 className="content-base font-semibold">{t_i18n("Cases")}</h2>
            <p className="content-compact text-default-secondary" title={`${exactCount(intl.locale, openIssues)} / ${exactCount(intl.locale, openCases.length)}`}>
              {t_i18n("{count, plural, one {# open case} other {# open cases}}", { count: openCases.length })}
              {" · "}
              {t_i18n("{count, plural, one {# open issue} other {# open issues}}", { count: openIssues })}
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <SegmentedFilter<Period>
              selection="radio"
              size="sm"
              ariaLabel={t_i18n("Period")}
              value={periodKey}
              onChange={setPeriodKey}
              options={PERIODS.map((p) => ({ key: p, label: t_i18n("{days} d", { days: p }) }))}
            />
            <Button size="sm" onClick={() => navigate("/ctem/cases")}>
              {t_i18n("View all cases")}
            </Button>
          </div>
        </div>

        {cases.length === 0 ? (
          <p className="flex items-center gap-2 content-base text-default-secondary" data-testid="ctem-cases-board-empty">
            {empty.unread ? (
              <Icon name="triangle-alert" size={16} className="shrink-0 text-feedback-warning-primary" aria-hidden />
            ) : (
              <Icon name="circle-check" size={16} className="shrink-0 text-feedback-success-primary" aria-hidden />
            )}
            {empty.label}
          </p>
        ) : (
          <div className={cn("grid gap-3", BOARD_GRID)} data-columns={CTEM_CASE_STATES.length}>
            {CTEM_CASE_STATES.map((state) => {
              const list = byState[state];
              const open = isOpenCaseState(state);
              const overdue = open ? list.filter((c) => c.due_at && Date.parse(c.due_at) < now).length : 0;
              const expiring =
                state === "accepted_risk"
                  ? list.filter((c) => c.accepted_until && Date.parse(c.accepted_until) - now <= 14 * DAY).length
                  : 0;
              return (
                <section
                  key={state}
                  aria-label={t_i18n(CASE_STATE_LABEL[state])}
                  className={cn("flex min-w-0 flex-col gap-3 rounded-sm border border-elevation-subtle layer-2 p-3 light:layer-1", BOARD_COLUMN_SURFACE)}
                  data-testid={`ctem-cases-board-${state}`}
                >
                  <p className="h-4 truncate content-caption text-default-secondary">{inflowLine(state, enteredIn(list).length, period, t_i18n)}</p>
                  <div className="flex items-center justify-between gap-2">
                    <CaseStateChip state={state} />
                    <span className="text-xl font-semibold tabular-nums">{exactCount(intl.locale, list.length)}</span>
                  </div>
                  {open && <AgeingBar cases={list} now={now} />}
                  {overdue > 0 && (
                    <p className={cn("flex items-center gap-1 content-caption", OVERDUE_TEXT)}>
                      <Icon name="circle-alert" size={16} className="text-feedback-error-primary" />
                      {t_i18n("{count, plural, one {# overdue} other {# overdue}}", { count: overdue })}
                    </p>
                  )}
                  {expiring > 0 && (
                    <p className="flex items-center gap-1 content-caption text-feedback-warning-primary">
                      <Icon name="clock" size={16} />
                      {t_i18n("{count, plural, one {# expires in 14 days} other {# expire in 14 days}}", { count: expiring })}
                    </p>
                  )}
                  {open && list.length > 0 && (
                    <ul className="-mx-2 space-y-0.5">
                      {topCases(list).map((c) => (
                        <TopCase key={c.id} c={c} owner={ownerName(c.owner_user_id)} now={now} />
                      ))}
                    </ul>
                  )}
                </section>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
