/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The Improvements inbox (#8261): the proposals the viewer may read, from
 * `GET /health/improvements` (level 3's read rule: who may manage the
 * target). On the platform page it lists every proposal; on an agent or flow
 * page, that target's own. A proposal's measured result is shown as measured:
 * `unmeasured` is said, never read as a gain.
 *
 * On the shared data table in server mode (#9378, slice 6): search in the
 * summaries, Type and Status pills, sort by date, type or status, page size and
 * CSV, the state in the URL (`imp_` keys). A row opens in place on the proposal.
 */
import { useCallback, useMemo, useState, type ComponentProps } from "react";
import { ChevronDown, ChevronRight, Lightbulb } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { Tip } from "@/components/ui/tooltip";
import { useHealthName, useImprovements } from "@/hooks/api/useHealthApi";
import { useClampPage } from "@/hooks/useLibraryParams";
import { useFormatter } from "@/i18n";
import {
  IMPROVEMENT_STATUS_LABELS,
  IMPROVEMENT_TYPE_LABELS,
  IMPROVEMENT_VERDICT_LABELS,
  IMPROVEMENT_VERDICT_VARIANTS,
  healthTargetHref,
  labelOf,
} from "@/lib/health";
import { readTableState, writeTableState, type DataTableDefaults, type DataTableSort } from "@/lib/data-table-state";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { toggleArrayItem } from "@/lib/utils";
import type { ImprovementListFilters, ImprovementListItem } from "@/types";
import { ImprovementProposalView } from "./ImprovementProposalView";
import { Spinner } from "@filigran/design-system";

const PAGE_SIZE = 25;
/** The table's keys in the URL: `imp_q`, `imp_sort`, `imp_page`, `imp_size`, `imp_f_type`, `imp_f_status`. */
const URL_KEY = "imp_";
const DEFAULT_SORT: DataTableSort = { key: "date", direction: "desc" };
const DEFAULTS: DataTableDefaults = { sort: DEFAULT_SORT, pageSize: PAGE_SIZE };
/** The columns the server orders by (the API's `sort` values), and the direction a first click sorts. */
const SORT_FIRST = { date: "desc", type: "asc", status: "asc" } as const;
type ImprovementSort = keyof typeof SORT_FIRST;
const isSort = (key: string): key is ImprovementSort => key in SORT_FIRST;

/**
 * Column widths in px under the fixed layout; Summary takes the rest and cuts with its tooltip.
 * The inbox is drawn in three boxes (the page, the agent card, the 384 px flow builder panel):
 * Verdict steps aside under 896 px of table width, Type under 768, Status under 576, so
 * Summary keeps at least 200 px with no sideways scroll; the opened row shows all three
 * (UX round 1 on #10399, B2).
 */
const COLUMN_WIDTHS = { date: 144, type: 160, verdict: 176, status: 224 } as const;

/**
 * A one-line badge cell whose label may be cut by its column: the badge truncates and the
 * design-system Tip carries the whole label (UX round 2 on #10399, B1, A1, A2: the longest fr
 * status "Conservé après une modification humaine" does not fit 224 px). Every Type, Verdict
 * and Status cell is a badge, so every row has one height whatever columns step aside (B2).
 */
function CellBadge({ label, variant = "outline" }: { label: string; variant?: ComponentProps<typeof Badge>["variant"] }) {
  return (
    <Tip content={label}>
      <Badge variant={variant} className="max-w-full whitespace-nowrap" data-testid="improvement-cell-badge">
        <span className="truncate">{label}</span>
      </Badge>
    </Tip>
  );
}

function ProposalFacts({ item }: { item: ImprovementListItem }) {
  const { t_i18n } = useFormatter();
  const verdict = item.experiment_verdict ?? "unmeasured";
  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="improvement-facts">
      <Badge variant="outline">{t_i18n(labelOf(IMPROVEMENT_TYPE_LABELS, item.proposal_type))}</Badge>
      <Badge variant={IMPROVEMENT_VERDICT_VARIANTS[verdict] ?? "outline"}>{t_i18n(labelOf(IMPROVEMENT_VERDICT_LABELS, verdict))}</Badge>
      <Badge variant="outline">{t_i18n(labelOf(IMPROVEMENT_STATUS_LABELS, item.status))}</Badge>
    </div>
  );
}

function ProposalDetail({ item }: { item: ImprovementListItem }) {
  const { t_i18n } = useFormatter();
  const simulated = item.experiment?.simulated_writes;
  const href = healthTargetHref(
    item.target_type === "flow" ? "flow" : item.target_type === "assignment" ? "assignment" : "agent",
    item.target_type === "skill" ? item.agent_id : item.target_id,
  );
  return (
    <div className="space-y-2 py-1">
      <ProposalFacts item={item} />
      {item.summary && <p className="content-compact whitespace-pre-wrap text-default-primary">{item.summary}</p>}
      {typeof simulated === "number" && (
        <p className="content-compact text-default-secondary">
          {t_i18n("{count, plural, one {# write was simulated during the experiment.} other {# writes were simulated during the experiment.}}", { count: simulated })}
        </p>
      )}
      <ImprovementProposalView proposalId={item.id} />
      {href && (
        <Link to={href} className="content-compact text-accent hover:underline">
          {t_i18n("Open the target")}
        </Link>
      )}
    </div>
  );
}

export function ImprovementsInbox({ fixed }: { fixed?: ImprovementListFilters }) {
  const { t_i18n, intl } = useFormatter();
  const healthName = useHealthName();
  const [params, setParams] = useSearchParams();
  // The table keeps its state in the URL: read it here to ask the API for that page.
  const state = readTableState(params, URL_KEY, DEFAULTS);
  const sort = state.sort && isSort(state.sort.key) ? state.sort : DEFAULT_SORT;
  const filters: ImprovementListFilters = {
    ...fixed,
    proposal_type: state.filters.type?.length ? state.filters.type : undefined,
    status: state.filters.status?.length ? state.filters.status : undefined,
    q: state.search.trim() || undefined,
    sort: sort.key as ImprovementSort,
    direction: sort.direction,
    limit: state.pageSize,
    offset: state.page * state.pageSize,
  };
  const { data, isLoading, isFetching, isError, refetch } = useImprovements(filters);
  const total = data?.total;
  // Every proposal of the inbox's target, before the type, status and search: "n of N" once they narrow it (#12039).
  const everything = useImprovements({ ...fixed, limit: 1, offset: 0 }).data?.total;
  const setPage = useCallback(
    (page: number) =>
      setParams((previous) => writeTableState(previous, URL_KEY, { ...readTableState(previous, URL_KEY, DEFAULTS), page }, DEFAULTS), {
        replace: true,
      }),
    [setParams],
  );
  // A decision can move the last proposal of the last page out of a status filter.
  useClampPage(state.page, state.pageSize, total, setPage);

  const [open, setOpen] = useState<string[]>([]);
  const toggle = useCallback((id: string) => setOpen((ids) => toggleArrayItem(ids, id)), []);

  const tableFilters = useMemo<DataTableFilter<ImprovementListItem>[]>(
    () => [
      {
        key: "type",
        label: "Type",
        options: Object.entries(IMPROVEMENT_TYPE_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
      },
      {
        key: "status",
        label: "Status",
        options: Object.entries(IMPROVEMENT_STATUS_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
      },
    ],
    [t_i18n],
  );

  const columns = useMemo<DataTableColumn<ImprovementListItem>[]>(() => {
    // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
    const sortable = (key: ImprovementSort) => ({ sortValue: () => null, firstSort: SORT_FIRST[key] });
    const typeLabel = (item: ImprovementListItem) => t_i18n(labelOf(IMPROVEMENT_TYPE_LABELS, item.proposal_type));
    const verdictLabel = (item: ImprovementListItem) =>
      t_i18n(labelOf(IMPROVEMENT_VERDICT_LABELS, item.experiment_verdict ?? "unmeasured"));
    const statusLabel = (item: ImprovementListItem) => t_i18n(labelOf(IMPROVEMENT_STATUS_LABELS, item.status));
    return [
      {
        key: "date",
        header: "When",
        ...sortable("date"),
        interactive: true,
        width: COLUMN_WIDTHS.date,
        className: "whitespace-nowrap text-default-secondary",
        cell: (item) => {
          const isOpen = open.includes(item.id);
          const Chevron = isOpen ? ChevronDown : ChevronRight;
          return (
            <button
              type="button"
              onClick={() => toggle(item.id)}
              aria-expanded={isOpen}
              aria-label={t_i18n(isOpen ? "Collapse" : "Expand")}
              className="flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              <Chevron className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <Tip content={formatShortDateTimeIntl(intl, item.created_at)}>
                <span>{formatTimeAgo(intl, item.created_at)}</span>
              </Tip>
            </button>
          );
        },
        csv: (item) => item.created_at,
      },
      {
        key: "type",
        header: "Type",
        ...sortable("type"),
        width: COLUMN_WIDTHS.type,
        hideBelow: "3xl",
        hideable: true,
        cell: (item) => <CellBadge label={typeLabel(item)} />,
        csv: (item) => item.proposal_type,
      },
      {
        key: "verdict",
        header: "Verdict",
        width: COLUMN_WIDTHS.verdict,
        hideBelow: "4xl",
        hideable: true,
        cell: (item) => (
          <CellBadge
            label={verdictLabel(item)}
            variant={IMPROVEMENT_VERDICT_VARIANTS[item.experiment_verdict ?? "unmeasured"] ?? "outline"}
          />
        ),
        csv: (item) => item.experiment_verdict ?? "unmeasured",
      },
      {
        key: "status",
        header: "Status",
        ...sortable("status"),
        width: COLUMN_WIDTHS.status,
        hideBelow: "xl",
        hideable: true,
        // One thing per cell (UX round 1 on #10399, B1): a proposal the viewer decides on says
        // so alone, any other its status, both as a badge whose Tip carries a cut label (round 2).
        cell: (item) =>
          item.can_decide ? (
            <CellBadge label={t_i18n("Awaiting your decision")} variant="cyan" />
          ) : (
            <CellBadge label={statusLabel(item)} />
          ),
        csv: (item) => item.status,
      },
      {
        key: "summary",
        header: "Summary",
        className: "max-w-0 text-default-secondary",
        // One line, like every other cell: the opened row shows the whole summary.
        cell: (item) => (
          <span className="block truncate" title={item.summary ?? undefined}>
            {item.summary ?? "-"}
          </span>
        ),
        csv: (item) => item.summary ?? "",
      },
    ];
  }, [intl, open, t_i18n, toggle]);

  const narrowed = state.search.trim() !== "" || Object.values(state.filters).some((values) => values.length > 0);
  if (!isLoading && !isError && total === 0 && !narrowed) {
    return (
      <EmptyState
        icon={Lightbulb}
        title={t_i18n("No improvement proposal")}
        description={t_i18n("Proposals {name} files appear here for who may approve them.", { name: healthName })}
      />
    );
  }
  return (
    <DataTable
      rows={data?.items ?? []}
      columns={columns}
      rowKey={(item) => item.id}
      server={{ total: total ?? 0 }}
      totalAll={everything}
      defaultSort={DEFAULT_SORT}
      defaultPageSize={PAGE_SIZE}
      urlKey={URL_KEY}
      searchPlaceholder={t_i18n("Search proposals...")}
      filters={tableFilters}
      exportName="health-improvements"
      isLoading={isLoading}
      isError={isError}
      errorLabel={t_i18n("The improvement proposals could not be loaded.")}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No improvement proposal")}
      noResultsLabel={t_i18n("No proposal matches these filters.")}
      onRowClick={(item) => toggle(item.id)}
      toggleInCell
      rowClassName={(item) => (open.includes(item.id) ? "bg-elevation-hover" : undefined)}
      expandedRow={(item) => (open.includes(item.id) ? <ProposalDetail item={item} /> : null)}
      toolbar={isFetching && !isLoading && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
      layout="fixed"
      rowTestId="improvement-row"
    />
  );
}
