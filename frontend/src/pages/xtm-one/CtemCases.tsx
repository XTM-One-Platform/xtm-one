// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM Cases list `/ctem/cases` (dev-docs/ctem-cases.md section 7.2): the
 * open exposures grouped by the fix they share, as the caller sees them. The
 * API returns only the cases the caller sees at least one issue of, with
 * counts computed on those (#11613), so the tabs and the total never count a
 * case the caller cannot open.
 */
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useNow } from "@/hooks/useTick";
import { caseDueFlags } from "./ctem/cases-due";
import { CASE_CREATED_RANGES, CASE_CREATED_RANGE_LABEL, caseCreatedRanges } from "./ctem/cases-created-range";
import { caseAttention, caseOpenedAt } from "./ctem/case-attention";
import { CasesAttentionBand } from "./ctem/CasesAttentionBand";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import {
  CTEM_CASE_STATES,
  useCtemCases,
  type CtemCaseState,
  type CtemCaseSummary,
} from "@/hooks/api/useCtemCasesApi";
import { CasesBulkActions } from "./ctem/CasesBulkActions";
import { CasesFreshness } from "./ctem/CasesFreshness";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
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

type StateTab = CtemCaseState | "all";

/** The tab a `?state=` opens; an unknown or absent value opens Needs attention. */
function initialStateTab(param: string | null): StateTab {
  if (param === "all") return "all";
  return (CTEM_CASE_STATES as readonly string[]).includes(param ?? "") ? (param as CtemCaseState) : "needs_attention";
}

export default function CtemCases() {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError, refetch } = useCtemCases();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTabState] = useState<StateTab>(() => initialStateTab(searchParams.get("state")));
  // The tab is in the URL (`?state=`): the command center widget opens the list on a state.
  const setTab = (next: StateTab) => {
    setTabState(next);
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        params.set("state", next);
        return params;
      },
      { replace: true },
    );
  };
  const [selected, setSelected] = useState<string[]>([]);

  const cases = useMemo(() => data?.cases ?? [], [data]);
  const now = useNow();
  const ownerIds = useMemo(() => [...new Set(cases.map((c) => c.owner_user_id).filter((id): id is string => Boolean(id)))], [cases]);
  const { data: owners } = useUserDirectoryByIds(ownerIds);
  const ownerName = (id: string | null) => (id ? (owners?.find((o) => o.id === id)?.full_name ?? null) : null);

  // A product that could not be read never yields the all-clear sentence (#12139).
  const empty = useCasesEmptyLabel(data?.details_unavailable);
  const counts = data?.counts;
  const total = counts ? CTEM_CASE_STATES.reduce((sum, s) => sum + (counts[s] ?? 0), 0) : undefined;
  const rows = tab === "all" ? cases : cases.filter((c) => c.state === tab);

  const DAY_MS = 24 * 3600 * 1000;
  const ageDays = (c: CtemCaseSummary) => {
    const opened = caseOpenedAt(c);
    return opened ? Math.max(0, Math.floor((now - Date.parse(opened)) / DAY_MS)) : null;
  };

  const columns: DataTableColumn<CtemCaseSummary>[] = [
    {
      // The default sort (#1176): one explained score, the band above names its reasons.
      key: "attention",
      header: "Attention",
      align: "right",
      hint: t_i18n("Priority, KEV, crown jewels touched, due date, owner and age, added up. Resolved cases and accepted risks score 0."),
      cell: (c) => formatCount(intl.locale, caseAttention(c, now).score),
      sortValue: (c) => caseAttention(c, now).score,
      csv: (c) => caseAttention(c, now).score,
      firstSort: "desc",
      hideable: true,
    },
    {
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
      // The Attention score already carries it: off by default at every width so the
      // default columns fit at 1728 in FR and ES (UX round 1 on #1243, B1);
      // the Columns picker brings it back.
      hideBelow: "6xl",
      hideable: true,
      defaultHidden: true,
    },
    {
      key: "crown_jewels",
      header: "Crown jewels",
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
      header: "State",
      // The chip sets the width (never cut, never a fixed 14rem that scrolls the
      // table at 1080 in FR): UX round 1 on #897, B1.
      className: "whitespace-nowrap",
      cell: (c) => <CaseStateChip state={c.state} />,
      sortValue: (c) => CTEM_CASE_STATES.indexOf(c.state),
      csv: (c) => c.state,
    },
  ];

  return (
    <div className="space-y-6">
      {/* The one CTEM header band (#11660): the definition behind the "i".
          Cases is a row of the CTEM menu group, titled like Agents or Fleets:
          no eyebrow, no back link, no refresh button (cases recompute by
          themselves, dev-docs/ctem-cases.md s.4.3); the freshness line says when. */}
      <CtemPageHeader
        title={t_i18n("Cases")}
        hint={t_i18n("Open issues grouped by the fix that closes them. You see the cases holding at least one issue you can see in OpenCTI or OpenAEV.")}
        hintLabel={t_i18n("About cases")}
        actions={<CasesFreshness freshness={data?.freshness} now={now} />}
      />

      <CasesAttentionBand cases={cases} now={now} />

      <SegmentedFilter<StateTab>
        selection="radio"
        overflow="menu"
        ariaLabel={t_i18n("Case state")}
        value={tab}
        onChange={(next) => {
          setTab(next);
          setSelected([]);
        }}
        options={[
          ...CTEM_CASE_STATES.map((s) => ({
            key: s as StateTab,
            label: t_i18n(CASE_STATE_LABEL[s]),
            count: counts?.[s],
            countPending: !counts,
            hint: s === "resolved" ? "All time" : undefined,
          })),
          { key: "all", label: t_i18n("All"), count: total, countPending: total === undefined },
        ]}
      />

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(c) => c.id}
        rowHref={(c) => `/ctem/cases/${c.id}`}
        selection={{
          selected,
          onChange: setSelected,
          actions: (
            <CasesBulkActions
              cases={cases.filter((c) => selected.includes(c.id))}
              onDone={() => setSelected([])}
            />
          ),
        }}
        defaultSort={{ key: "attention", direction: "desc" }}
        defaultPageSize={25}
        searchFields={(c) => [c.title, c.product_label, c.fixed_version]}
        searchPlaceholder={t_i18n("Search fix, product or version")}
        filtersInPill
        filters={[
          {
            key: "priority",
            label: "Priority",
            values: (c) => [c.priority],
            options: (["critical", "high", "medium", "low"] as const).map((p) => ({ value: p, label: t_i18n(CASE_PRIORITY_LABEL[p]) })),
            // Ordinal scale: critical to low, never alphabetical (#12289).
            ordered: true,
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
            key: "crown_jewels",
            label: "Crown jewels",
            values: (c) => [c.crown_jewel_count > 0 ? "yes" : "no"],
            options: [
              { value: "yes", label: t_i18n("Touches a crown jewel") },
              { value: "no", label: t_i18n("No crown jewel") },
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
