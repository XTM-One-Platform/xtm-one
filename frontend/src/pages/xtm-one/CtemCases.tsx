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
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useToast } from "@/components/ui/toast";
import { useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import {
  CTEM_CASE_STATES,
  useCtemCases,
  useRecomputeCtemCases,
  type CtemCaseState,
  type CtemCaseSummary,
} from "@/hooks/api/useCtemCasesApi";
import { CasesBulkActions } from "./ctem/CasesBulkActions";
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
  const { toast } = useToast();
  const { data, isLoading, isError, refetch } = useCtemCases();
  const recompute = useRecomputeCtemCases();
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

  const runRecompute = () =>
    recompute.mutate(undefined, {
      onSuccess: (result) =>
        toast(
          result.applied
            ? { title: t_i18n("Cases recomputed"), variant: "success" }
            : {
                title: t_i18n("Nothing recomputed"),
                description: t_i18n("{products} did not answer: no case was changed.", { products: (result.missing ?? []).join(", ") }),
                variant: "info",
              },
        ),
      onError: (error) => toast({ title: t_i18n("Recompute failed"), description: error.message, variant: "error" }),
    });

  const columns: DataTableColumn<CtemCaseSummary>[] = [
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
      cell: (c) => <TruncatedText text={c.title} className="max-w-[28rem] font-medium" />,
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
      key: "owner",
      header: "Owner",
      cell: (c) => <span className="block max-w-[12rem]"><CaseOwner name={ownerName(c.owner_user_id)} /></span>,
      sortValue: (c) => (ownerName(c.owner_user_id) ?? "").toLowerCase(),
      csv: (c) => ownerName(c.owner_user_id) ?? "",
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
      key: "state",
      header: "State",
      cell: (c) => <CaseStateChip state={c.state} />,
      sortValue: (c) => CTEM_CASE_STATES.indexOf(c.state),
      csv: (c) => c.state,
    },
  ];

  return (
    <div className="space-y-6">
      {/* The one CTEM header band (#11660): the definition behind the "i". */}
      <CtemPageHeader
        back={{ to: "/ctem/remediation", label: "Back to Remediation" }}
        title={t_i18n("Cases")}
        hint={t_i18n("Open issues grouped by the fix that closes them. You see the cases holding at least one issue you can see in OpenCTI or OpenAEV.")}
        hintLabel={t_i18n("About cases")}
        actions={
          <Button variant="outline" className="gap-1.5" onClick={runRecompute} disabled={recompute.isPending}>
            <Icon name="refresh-cw" size={16} /> {t_i18n("Recompute")}
          </Button>
        }
      />

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
        defaultSort={{ key: "priority", direction: "desc" }}
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
