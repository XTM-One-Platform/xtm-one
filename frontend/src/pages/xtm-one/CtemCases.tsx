// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM Cases list `/ctem/cases` (dev-docs/ctem-cases.md section 7.2): the
 * exposures grouped by the fix they share, as the caller sees them. The API
 * returns only the cases the caller sees at least one issue of, with counts
 * computed on those (#11613), so the chips never count a case the caller
 * cannot open. One full list (#1503): no state tabs and no "Act on this first"
 * band; quick filter chips above the table (`?state=` scope, `?c_q=` toggles),
 * open cases by default, so a case paused on a person shows on landing.
 * A List / Kanban switch (`/ctem/cases/kanban`) draws the same rows as status
 * columns, or CTEM phase columns when grouped by phase (CasesKanban).
 */
import { useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useNow } from "@/hooks/useTick";
import { caseDueFlags } from "./ctem/cases-due";
import { CASE_CREATED_RANGES, CASE_CREATED_RANGE_LABEL, caseCreatedRanges } from "./ctem/cases-created-range";
import { caseOpenedAt } from "./ctem/case-attention";
import {
  CASE_QUICK_FILTER_LABEL,
  CASE_SCOPES,
  CASE_TOGGLES,
  applyCaseQuickFilters,
  caseQuickFilterCount,
  caseWaitFacet,
  isStalledCase,
  readCaseScope,
  readCaseToggles,
  writeCaseToggles,
  type CaseScope,
  type CaseScopeChip,
  type CaseToggle,
} from "./ctem/cases-quick-filters";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { DataTable, type DataTableColumn, type DataTableGroupBy } from "@/components/ui/data-table";
import { caseGroupLabel, caseGroupRank, caseGroupValue, readCaseGroupBy, type CaseGroupBy } from "./ctem/cases-grouping";
import { CasesGroupByPill } from "./ctem/CasesGroupByPill";
import { CasesKanban } from "./ctem/CasesKanban";
import { caseViewPath, readCaseView, type CaseView } from "./ctem/cases-kanban";
import { CasesViewsPill } from "./ctem/CasesViewsPill";
import { QuickFilterChips } from "@/components/ui/quick-filter-chips";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { caseWaitWho, caseWaitWhoLabel } from "./ctem/case-wait";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useMe, useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";
import { CTEM_CASE_STATES, useCtemCases, type CtemCaseOpenedBy, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CasesBulkActions } from "./ctem/CasesBulkActions";
import { CasesDeleteAction } from "./ctem/CasesDeleteAction";
import { CasesFreshness } from "./ctem/CasesFreshness";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import {
  CASE_PRIORITY_LABEL,
  CASE_PRIORITY_RANK,
  CASE_STATE_LABEL,
  CasePriorityChip,
  CaseOwner,
  CaseStateChip,
  useCasesEmptyLabel,
} from "./ctem/cases-shared";
import { CTEM_STAGE_CONFIG, CTEM_STAGE_KEYS } from "@/lib/constants";

/** The "Opened by" filter values (#1723), as `ctem_case_delete.origin` answers them. */
const CASE_OPENED_BY = ["signal", "split", "legacy"] as const satisfies readonly CtemCaseOpenedBy[];
/** Filter option labels. @i18n-keys */
const CASE_OPENED_BY_LABEL = {
  signal: "A signal",
  split: "A split",
  legacy: "Legacy",
} as const satisfies Record<CtemCaseOpenedBy, string>;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default function CtemCases() {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError, refetch } = useCtemCases();
  const { data: me } = useMe();
  const { isAdmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const setParam = (key: string, value: string | null) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (value) params.set(key, value);
        else params.delete(key);
        return params;
      },
      { replace: true },
    );
  // The scope is in the URL (`?state=`): the command center opens the list on a status, a saved view
  // (CasesViewsPill) sets it; absent, an unknown or a first-model value, it is the open cases.
  const scope = readCaseScope(searchParams.get("state"));
  const toggles = readCaseToggles(searchParams.get("c_q"));
  const [selected, setSelected] = useState<string[]>([]);
  const setScope = (next: CaseScope) => {
    setParam("state", next === "open" ? null : next);
    setSelected([]);
  };
  const flipToggle = (key: CaseToggle) => {
    const next = toggles.includes(key) ? toggles.filter((t) => t !== key) : [...toggles, key];
    setParam("c_q", writeCaseToggles(next) || null);
    setSelected([]);
  };
  // The grouping is in the URL too (`?c_group=`), so a link keeps it.
  const groupKey = readCaseGroupBy(searchParams.get("c_group"));
  const setGroupKey = (next: CaseGroupBy) => setParam("c_group", next === "none" ? null : next);
  // The view is the path (`/ctem/cases/kanban`, #10203), the query kept; the Kanban lays its
  // columns out by phase when grouped by phase.
  const location = useLocation();
  const navigate = useNavigate();
  const view = readCaseView(location.pathname);
  const setView = (next: CaseView) => navigate({ pathname: caseViewPath(next), search: location.search });
  const kanbanBy = groupKey === "phase" ? "phase" : "state";

  const cases = useMemo(() => data?.cases ?? [], [data]);
  const now = useNow();
  const meId = me?.id ?? null;
  // Owners, and the people a case waiting for decision waits on (a user id the loop wrote on its gate).
  const ownerIds = useMemo(
    () => [...new Set(cases.flatMap((c) => [c.owner_user_id, c.waiting_on?.by ?? null]).filter((id): id is string => Boolean(id) && UUID.test(id ?? "")))],
    [cases],
  );
  const { data: owners } = useUserDirectoryByIds(ownerIds);
  const ownerName = (id: string | null) => (id ? (owners?.find((o) => o.id === id)?.full_name ?? null) : null);

  // A product that could not be read never yields the all-clear sentence (#12139).
  const empty = useCasesEmptyLabel(data?.details_unavailable);
  const rows = useMemo(() => applyCaseQuickFilters(cases, scope, toggles, now, meId), [cases, scope, toggles, now, meId]);
  const countOf = (chip: CaseScopeChip | CaseToggle) => (data ? caseQuickFilterCount(chip, cases, scope, toggles, now, meId) : undefined);
  const groupBy = useMemo<DataTableGroupBy<CtemCaseSummary> | undefined>(() => {
    if (groupKey === "none" || view === "kanban") return undefined;
    return {
      value: (c) => caseGroupValue(groupKey, c),
      rank: caseGroupRank(groupKey),
      label: (value) =>
        groupKey === "owner"
          ? value
            ? (owners?.find((o) => o.id === value)?.full_name ?? t_i18n("Unknown user"))
            : t_i18n("Unassigned")
          : t_i18n(caseGroupLabel(groupKey, value)),
    };
  }, [groupKey, owners, t_i18n, view]);

  const DAY_MS = 24 * 3600 * 1000;
  const ageDays = (c: CtemCaseSummary) => {
    const opened = caseOpenedAt(c);
    return opened ? Math.max(0, Math.floor((now - Date.parse(opened)) / DAY_MS)) : null;
  };

  const columns: DataTableColumn<CtemCaseSummary>[] = [
    {
      // The default sort (#1503): priority, critical first; the attention score and its band are gone.
      key: "priority",
      header: "Priority",
      cell: (c) => <CasePriorityChip priority={c.priority} />,
      sortValue: (c) => CASE_PRIORITY_RANK[c.priority],
      csv: (c) => c.priority,
      firstSort: "desc",
    },
    {
      key: "fix",
      header: "Fix",
      // The title gives way first at 1080 so the State chip is never cut and the
      // table never scrolls (#865 prod retest D2; UX round 1 on #897, B1).
      // 22rem at 1280 and up keeps every default column inside the container at 1728 in FR
      // (prod retest of #1182, gap ctem-cases-fr-columns-overflow).
      cell: (c) => <TruncatedText text={c.title} className="max-w-[15rem] min-[1280px]:max-w-[22rem] font-medium" />,
      sortValue: (c) => c.title.toLowerCase(),
      csv: (c) => c.title,
    },
    {
      key: "issues",
      header: "Issues",
      align: "right",
      hint: t_i18n("Open issues the fix closes. Issues you cannot see in their source product are counted as restricted."),
      cell: (c) => (
        <span className="whitespace-nowrap">
          {formatCount(intl.locale, c.open_issue_count)}
          {c.restricted_count > 0 && (
            <span className="ml-1 text-default-secondary">
              ({t_i18n("{count, plural, one {# restricted} other {# restricted}}", { count: c.restricted_count })})
            </span>
          )}
        </span>
      ),
      sortValue: (c) => c.open_issue_count,
      csv: (c) => c.open_issue_count,
      firstSort: "desc",
      // Off by default at every width so the
      // default columns fit at 1728 in FR and ES (UX round 1 on #1243, B1);
      // the Columns picker brings it back.
      hideBelow: "6xl",
      hideable: true,
      defaultHidden: true,
    },
    {
      key: "crown_jewels",
      header: "Primary assets",
      align: "right",
      cell: (c) => formatCount(intl.locale, c.crown_jewel_count),
      sortValue: (c) => c.crown_jewel_count,
      csv: (c) => c.crown_jewel_count,
      firstSort: "desc",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "kev",
      header: "KEV",
      align: "right",
      cell: (c) => formatCount(intl.locale, c.kev_count),
      sortValue: (c) => c.kev_count,
      csv: (c) => c.kev_count,
      firstSort: "desc",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      cell: (c) => formatCount(intl.locale, c.asset_count),
      sortValue: (c) => c.asset_count,
      csv: (c) => c.asset_count,
      firstSort: "desc",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "signals",
      header: "Signals",
      align: "right",
      hint: t_i18n("Signals that opened or joined the case."),
      cell: (c) => formatCount(intl.locale, c.signal_count ?? 0),
      sortValue: (c) => c.signal_count ?? 0,
      csv: (c) => c.signal_count ?? 0,
      firstSort: "desc",
      // Off by default so every default column fits at 1728 and 1080 (UX round 1 on #1182, B1);
      // the Columns picker brings it back.
      hideBelow: "6xl",
      hideable: true,
      defaultHidden: true,
    },
    {
      key: "owner",
      header: "Owner",
      cell: (c) => <span className="block max-w-[10rem]"><CaseOwner name={ownerName(c.owner_user_id)} /></span>,
      sortValue: (c) => (ownerName(c.owner_user_id) ?? "").toLowerCase(),
      csv: (c) => ownerName(c.owner_user_id) ?? "",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "due",
      header: "Due",
      cell: (c) =>
        c.due_at ? (
          <span className="whitespace-nowrap">{intl.formatDate(c.due_at, { day: "numeric", month: "short" })}</span>
        ) : (
          <span className="text-default-secondary">-</span>
        ),
      sortValue: (c) => c.due_at ?? "",
      csv: (c) => c.due_at ?? "",
      hideable: true,
    },
    {
      key: "created",
      header: "Created",
      cell: (c) => <DateTimeCell value={caseOpenedAt(c)} />,
      sortValue: (c) => caseOpenedAt(c) ?? "",
      csv: (c) => caseOpenedAt(c) ?? "",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "age",
      header: "Age",
      align: "right",
      cell: (c) => {
        const days = ageDays(c);
        return days === null ? "-" : <span className="whitespace-nowrap">{t_i18n("{days, plural, one {# day} other {# days}}", { days })}</span>;
      },
      sortValue: (c) => ageDays(c) ?? -1,
      csv: (c) => ageDays(c) ?? "",
      firstSort: "desc",
      // Created already says it (UX round 1 on #1182, B1 and A2): off by default, never below 6xl.
      hideBelow: "6xl",
      hideable: true,
      defaultHidden: true,
    },
    {
      key: "last_change",
      header: "Last change",
      cell: (c) => <DateTimeCell value={c.updated_at ?? c.state_since} />,
      sortValue: (c) => c.updated_at ?? c.state_since ?? "",
      csv: (c) => c.updated_at ?? c.state_since ?? "",
      firstSort: "desc",
      hideBelow: "6xl",
      hideable: true,
    },
    {
      key: "state",
      header: "Status",
      // The chip sets the width (never cut, never a fixed 14rem that scrolls the
      // table at 1080 in FR): UX round 1 on #897, B1.
      className: "whitespace-nowrap",
      cell: (c) => <CaseStateChip state={c.state} />,
      sortValue: (c) => CTEM_CASE_STATES.indexOf(c.state),
      csv: (c) => c.state,
    },
    {
      // Who a case waiting for decision waits on, and since when (#1503): the gate the loop parked it on.
      key: "waits_on",
      header: "Waits on",
      cell: (c) => {
        const wait = c.state === "waiting_decision" ? c.waiting_on : null;
        // An open case the sweep found waiting on nobody says so (#1736): no gate, no reminder, nothing moved it since.
        if (!wait && isStalledCase(c) && c.stalled_since) {
          return (
            <span className="block max-w-[14rem] text-feedback-warning-primary" data-testid="ctem-case-waits-on-nobody">
              <TruncatedText text={t_i18n("Nobody, since {date}", { date: intl.formatDate(c.stalled_since, { day: "numeric", month: "short" }) })} />
            </span>
          );
        }
        if (!wait) return <span className="text-default-secondary">-</span>;
        // Never the raw `by`: a person by name, a role or policy in words, else "a person" (UX r1 B3 on #1515).
        const who = caseWaitWhoLabel(caseWaitWho(wait.by, ownerName), t_i18n);
        return (
          <span className="block max-w-[14rem]">
            <TruncatedText
              text={wait.since ? t_i18n("{who}, since {date}", { who, date: intl.formatDate(wait.since, { day: "numeric", month: "short" }) }) : who}
            />
          </span>
        );
      },
      sortValue: (c) => (c.state === "waiting_decision" ? (c.waiting_on?.since ?? "") : isStalledCase(c) ? (c.stalled_since ?? "") : ""),
      csv: (c) =>
        c.state === "waiting_decision" && c.waiting_on
          ? caseWaitWhoLabel(caseWaitWho(c.waiting_on.by, ownerName), t_i18n)
          : isStalledCase(c)
            ? "nobody"
            : "",
      hideBelow: "6xl",
      hideable: true,
    },
  ];

  return (
    <div className="space-y-6">
      {/* The app's standard list header (#1367, Sam 23 Nov 2026: no band with a surface and a border): the
          definition behind the "i", no refresh button (cases recompute by themselves, dev-docs/ctem-cases.md
          s.4.3); the freshness line says when. */}
      <ListPageHeader
        title={t_i18n("Cases")}
        info={t_i18n("Open issues grouped by the fix that closes them. You see the cases holding at least one issue you can see in OpenCTI or OpenAEV.")}
        infoLabel={t_i18n("About cases")}
        actions={<CasesFreshness freshness={data?.freshness} now={now} />}
      />

      {/* The scope is one choice among four: the shared SegmentedFilter radio, as Signals draws its
          state scope (UX r1 B1 on #1515); the toggles beside it combine, as pressed chips. */}
      <div className="flex flex-wrap items-center gap-3" data-testid="ctem-cases-quick-filters">
        <SegmentedFilter<CaseScopeChip>
          selection="radio"
          overflow="menu"
          ariaLabel={t_i18n("Cases shown")}
          testId="ctem-cases-scope"
          value={(CASE_SCOPES as readonly string[]).includes(scope) ? (scope as CaseScopeChip) : ("" as CaseScopeChip)}
          onChange={setScope}
          options={CASE_SCOPES.map((key) => ({ key, label: CASE_QUICK_FILTER_LABEL[key], count: countOf(key), countPending: !data }))}
        />
        <QuickFilterChips<CaseToggle>
          testId="ctem-cases-toggles"
          label={t_i18n("Narrow the cases")}
          toggles={CASE_TOGGLES.map((key) => ({ key, label: t_i18n(CASE_QUICK_FILTER_LABEL[key]), count: countOf(key) }))}
          active={toggles}
          onToggle={flipToggle}
        />
      </div>

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(c) => c.id}
        rowHref={(c) => `/ctem/cases/${c.id}`}
        selection={{
          selected,
          onChange: setSelected,
          actions: (
            <>
              <CasesBulkActions
                cases={cases.filter((c) => selected.includes(c.id))}
                onDone={() => setSelected([])}
              />
              {/* Administrators only (#1723): the component draws nothing for anyone else. */}
              <CasesDeleteAction caseIds={selected} onDone={() => setSelected([])} />
            </>
          ),
        }}
        // The row's Delete is an administrator's (#1723): no actions column for anyone else.
        rowActions={isAdmin ? (c) => <CasesDeleteAction row caseIds={[c.id]} onDone={() => setSelected((s) => s.filter((id) => id !== c.id))} /> : undefined}
        rowActionsWidth={48}
        groupBy={groupBy}
        view={{ value: view, onChange: (mode) => setView(mode === "kanban" ? "kanban" : "list"), modes: ["list", "kanban"] }}
        // The Kanban draws every row the filters keep, never one page of them.
        paginate={view !== "kanban"}
        renderGrid={
          view === "kanban"
            ? (shown) => <CasesKanban cases={shown} by={kanbanBy} now={now} meId={meId} ownerName={ownerName} />
            : undefined
        }
        toolbar={
          <>
            <CasesGroupByPill value={groupKey} onChange={setGroupKey} />
            <CasesViewsPill />
          </>
        }
        defaultSort={{ key: "priority", direction: "desc" }}
        defaultPageSize={25}
        searchFields={(c) => [c.title, c.product_label, c.fixed_version]}
        searchPlaceholder={t_i18n("Search fix, product or version")}
        filtersInPill
        filters={[
          {
            // One status among the eight (#1503), beside the scope chips: the Kanban's columns, as a filter.
            key: "status",
            label: "Status",
            values: (c) => [c.state],
            options: CTEM_CASE_STATES.map((s) => ({ value: s, label: t_i18n(CASE_STATE_LABEL[s]) })),
            // Ordinal scale: the lifecycle order, new to accepted risk, never alphabetical.
            ordered: true,
          },
          {
            // The command center and the stage pages link here (`c_f_phase`, #1621): the open cases of one phase.
            key: "phase",
            label: "CTEM phase",
            values: (c) => (c.phase ? [c.phase] : []),
            options: CTEM_STAGE_KEYS.map((k) => ({ value: k, label: t_i18n(CTEM_STAGE_CONFIG[k].label) })),
            // Ordinal scale: the CTEM lifecycle order, scoping to remediation, never alphabetical.
            ordered: true,
          },
          {
            key: "priority",
            label: "Priority",
            values: (c) => [c.priority],
            options: (["critical", "high", "medium", "low"] as const).map((p) => ({ value: p, label: t_i18n(CASE_PRIORITY_LABEL[p]) })),
            // Ordinal scale: critical to low, never alphabetical (#12289).
            ordered: true,
          },
          {
            // How the case came to be (#1723): "Legacy" keeps the cases a source read before signals built.
            key: "opened_by",
            label: "Opened by",
            values: (c) => (c.opened_by ? [c.opened_by] : []),
            options: CASE_OPENED_BY.map((o) => ({ value: o, label: t_i18n(CASE_OPENED_BY_LABEL[o]) })),
          },
          {
            key: "owner",
            label: "Owner",
            values: (c) => [c.owner_user_id ? "assigned" : "unassigned"],
            options: [
              { value: "assigned", label: t_i18n("Assigned") },
              { value: "unassigned", label: t_i18n("Unassigned") },
            ],
          },
          {
            // The command center row links here (`c_f_due`): one definition of overdue and expiring (cases-due).
            key: "due",
            label: "Due",
            values: (c) => caseDueFlags(c, now),
            options: [
              { value: "overdue", label: t_i18n("Overdue") },
              { value: "expiring", label: t_i18n("Accepted risk expiring") },
            ],
          },
          {
            // Whom an open case waits on (#1736): a person (a gate of the loop) or nobody (the sweep found it stalled).
            key: "waits_on",
            label: "Waits on",
            values: (c) => caseWaitFacet(c),
            options: [
              { value: "person", label: t_i18n("A person") },
              { value: "nobody", label: t_i18n("Stalled (case filter)") },
            ],
          },
          {
            key: "crown_jewels",
            label: "Primary assets",
            values: (c) => [c.crown_jewel_count > 0 ? "yes" : "no"],
            options: [
              { value: "yes", label: t_i18n("Touches a primary asset") },
              { value: "no", label: t_i18n("No primary asset") },
            ],
          },
          {
            key: "kev",
            label: "KEV",
            values: (c) => [c.kev_count > 0 ? "yes" : "no"],
            options: [
              { value: "yes", label: t_i18n("Has a KEV issue") },
              { value: "no", label: t_i18n("No KEV issue") },
            ],
          },
          {
            // A case opened 3 days ago is in the last 7, 30 and 90 days (cases-created-range).
            key: "created",
            label: "Created",
            values: (c) => caseCreatedRanges(c, now),
            options: CASE_CREATED_RANGES.map((r) => ({ value: r, label: t_i18n(CASE_CREATED_RANGE_LABEL[r]) })),
            // Ordinal scale: the most recent range first, never alphabetical.
            ordered: true,
          },
        ]}
        exportName="ctem-cases"
        urlKey="c_"
        storageKey="ctem-cases"
        isLoading={isLoading && !data}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={empty.label}
        noResultsLabel={t_i18n("No case matches these filters")}
        rowTestId="ctem-cases-row"
      />
    </div>
  );
}
