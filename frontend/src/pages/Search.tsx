// SPDX-License-Identifier: Apache-2.0

/**
 * `/search?q=&type=` - the results page of the top bar search (#11201), laid
 * out as OpenAEV's full-text search: a rail of entity types with their match
 * counts ("All" first), and the hits of the chosen type in the shared data
 * table (name with its type icon, type, context, updated). The query and the
 * type live in the URL, so a refresh, a shared link and the back button keep
 * the view. Ranking, RBAC and paging are the server's (`GET /search`).
 */
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { TruncatedText } from "@/components/ui/truncated-text";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatTimeAgo } from "@/lib/relative-time";
import { useDebounce } from "@/hooks/useDebounce";
import {
  SEARCH_TYPES, MIN_SEARCH_LENGTH, useSearch, useSearchCounts, type SearchHit, type SearchSort, type SearchType,
} from "@/hooks/api/useSearchApi";
import { SEARCH_TYPE_VIEW, type LibraryIconName } from "@/lib/search-types";
import type { DataTableState } from "@/lib/data-table-state";

const PAGE_SIZE = 25;

/** The columns `GET /search` sorts (`sort_by`); none chosen = relevance. */
const SORT_KEYS: readonly string[] = ["name", "updated"];

const isSearchType = (value: string | null): value is SearchType =>
  value !== null && (SEARCH_TYPES as readonly string[]).includes(value);

export default function Search() {
  const { t_i18n, intl } = useFormatter();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const typeParam = params.get("type");
  const type = isSearchType(typeParam) ? typeParam : null;
  const [draft, setDraft] = useState(q);
  const [page, setPage] = useState({ index: 0, size: PAGE_SIZE });
  const [sort, setSort] = useState<SearchSort | null>(null);

  /* The field writes the URL (replace: typing is not a history entry). */
  const debounced = useDebounce(draft, 300);
  useEffect(() => {
    if (debounced !== draft || debounced.trim() === q.trim()) return;
    const next = new URLSearchParams(params);
    if (debounced.trim()) next.set("q", debounced.trim()); else next.delete("q");
    setPage((p) => ({ ...p, index: 0 }));
    setParams(next, { replace: true });
  }, [debounced, draft, q, params, setParams]);
  /* Back and forward change the URL under the field: the field follows. */
  useEffect(() => { setDraft(q); }, [q]);

  const counts = useSearchCounts(q);
  const results = useSearch(q, { type, limit: page.size, offset: page.index * page.size, sort });

  const selectType = (next: SearchType | null) => {
    const out = new URLSearchParams(params);
    if (next) out.set("type", next); else out.delete("type");
    setPage((p) => ({ ...p, index: 0 }));
    setParams(out);
  };

  const columns = useMemo<DataTableColumn<SearchHit>[]>(() => [
    {
      key: "name",
      header: "Name",
      // Server mode: the API sorts, the header only needs a sortValue to offer it.
      sortValue: () => null,
      cell: (hit) => (
        <span className="flex min-w-0 items-center gap-2">
          <Icon name={SEARCH_TYPE_VIEW[hit.type].icon} size={16} className="shrink-0 text-default-secondary" />
          <TruncatedText text={hit.name} />
        </span>
      ),
    },
    { key: "type", header: "Type", cell: (hit) => t_i18n(SEARCH_TYPE_VIEW[hit.type].singular), width: 160, hideable: true },
    {
      key: "context",
      header: "Context",
      // A block cut with its full text in a tooltip: an inline span ran past the cell over Updated (UX r1 B1).
      cell: (hit) => <TruncatedText text={hit.excerpt ?? ""} className="text-default-secondary" />,
      hideBelow: "4xl",
      hideable: true,
    },
    {
      key: "updated",
      header: "Updated",
      cell: (hit) => (hit.updated_at ? formatTimeAgo(intl, hit.updated_at) : ""),
      width: 140,
      sortValue: () => null,
      firstSort: "desc",
      hideBelow: "3xl",
      hideable: true,
    },
  ], [t_i18n, intl]);

  const onStateChange = (state: DataTableState) => {
    if (state.page !== page.index || state.pageSize !== page.size) setPage({ index: state.page, size: state.pageSize });
    const next: SearchSort | null = state.sort && SORT_KEYS.includes(state.sort.key)
      ? { key: state.sort.key as SearchSort["key"], direction: state.sort.direction }
      : null;
    if (next?.key !== sort?.key || next?.direction !== sort?.direction) setSort(next);
  };

  const total = counts.data?.total ?? 0;
  const tooShort = q.trim().length < MIN_SEARCH_LENGTH;

  return (
    <div className="flex flex-col gap-4">
      <ListPageHeader
        title={t_i18n("Search")}
        infoLabel={t_i18n("About search")}
        info={t_i18n("Every agent, fleet, conversation, work item, document and other object you can see, ranked by how well its name matches.")}
      />
      <div className="flex gap-6">
        <nav aria-label={t_i18n("Entity types")} className="flex w-[220px] shrink-0 flex-col gap-0.5">
          <TypeRow
            label={t_i18n("All")}
            icon="layout-dashboard"
            count={tooShort ? null : total}
            active={type === null}
            onClick={() => selectType(null)}
          />
          {SEARCH_TYPES.filter((key) => (counts.data?.counts[key] ?? 0) > 0 || key === type).map((key) => (
            <TypeRow
              key={key}
              label={t_i18n(SEARCH_TYPE_VIEW[key].plural)}
              icon={SEARCH_TYPE_VIEW[key].icon}
              count={counts.data?.counts[key] ?? 0}
              active={type === key}
              onClick={() => selectType(key)}
            />
          ))}
        </nav>
        <div className="min-w-0 flex-1">
          <DataTable<SearchHit>
            rows={tooShort ? [] : results.data?.items ?? []}
            columns={columns}
            rowKey={(hit) => `${hit.type}-${hit.id}`}
            rowHref={(hit) => SEARCH_TYPE_VIEW[hit.type].href(hit)}
            server={{ total: tooShort ? 0 : results.data?.total ?? 0 }}
            onStateChange={onStateChange}
            defaultPageSize={PAGE_SIZE}
            storageKey="search"
            search={{ value: draft, onChange: setDraft, placeholder: t_i18n("Search the platform") }}
            noFilters="the entity type rail beside the table is the filter"
            isLoading={!tooShort && results.isLoading}
            isError={results.isError}
            onRetry={() => void results.refetch()}
            emptyLabel={tooShort
              ? t_i18n("Type at least {count, plural, one {# character} other {# characters}} to search", { count: MIN_SEARCH_LENGTH })
              : t_i18n("No result")}
            layout="fixed"
            rowTestId="search-result-row"
          />
        </div>
      </div>
    </div>
  );
}

function TypeRow({ label, icon, count, active, onClick }: {
  label: string; icon: LibraryIconName; count: number | null; active: boolean; onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-current={active ? "true" : undefined}
      onClick={onClick}
      className={cn(
        "flex h-9 items-center gap-2 rounded-sm px-3 text-left content-base transition-colors",
        active ? "bg-elevation-hover text-default-primary" : "text-default-secondary hover:bg-elevation-hover",
      )}
    >
      <Icon name={icon} size={16} className="shrink-0" />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {count !== null && <span className="content-caption tabular-nums">{count}</span>}
    </button>
  );
}
