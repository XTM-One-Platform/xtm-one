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
 * Every document of the knowledge bases the user sees, through the shared data
 * table (#9378, slice 4). The server searches the file name and the knowledge
 * base name, filters by status, sorts and pages; the table's search, filter,
 * sort and page live in the URL (`doc_` keys) so reload and back keep the view.
 * `kb_ids` (the KB health warnings' links) scopes the list to some knowledge
 * bases and shows as a removable chip in the toolbar.
 */
import { useEffect, useMemo } from "react";
import { Icon, IconButton, Spinner } from "@filigran/design-system";
import { useSearchParams } from "react-router-dom";

import { BackButton } from "@/components/ui/back-button";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { filterPillClass } from "@/components/ui/filter-pill";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useGlobalKnowledgeDocuments, useReprocessDocument } from "@/hooks/api/useKnowledgeBaseApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { readTableState, writeTableState, type DataTableDefaults, type DataTableSort } from "@/lib/data-table-state";
import type { GlobalKnowledgeDocumentsResponse } from "@/types";

type GlobalDocument = GlobalKnowledgeDocumentsResponse["items"][number];
type DocumentSort = "created_at" | "filename" | "knowledge_base" | "status";

const PAGE_SIZE = 25;
const URL_KEY = "doc_";
const SCOPE_PARAM = "kb_ids";
const STATUSES = ["ready", "pending", "processing", "error"] as const;
const SORT_KEYS: readonly DocumentSort[] = ["created_at", "filename", "knowledge_base", "status"];
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };
const DEFAULTS: DataTableDefaults = { sort: DEFAULT_SORT, pageSize: PAGE_SIZE };

/**
 * Column widths in px under the fixed layout: the file name takes what is left,
 * a long knowledge base name is cut inside its column with the whole name in the
 * tooltip, so the page never scrolls sideways at 1080 (UX Director, retest of #10984).
 */
const COLUMN_WIDTHS = {
  knowledge_base: 224,
  source: 112,
  status: 128,
  chunks: 96,
  created_at: 144,
} as const;

const STATUS_LABELS: Record<(typeof STATUSES)[number], string> = {
  ready: "Ready",
  pending: "Pending",
  processing: "Processing",
  error: "Error",
};

function StatusBadge({ doc }: { doc: GlobalDocument }) {
  const { t_i18n } = useFormatter();
  const status = doc.status.toLowerCase();
  const variant = status === "ready" ? "success" : status === "error" ? "warning" : status === "processing" ? "cyan" : "outline";
  const label = status in STATUS_LABELS ? t_i18n(STATUS_LABELS[status as keyof typeof STATUS_LABELS]) : doc.status;
  const badge = <Badge variant={variant}>{label}</Badge>;
  if (!doc.error_message) return badge;
  // The error used to take a line of its own under the row; it is the badge's tooltip now.
  return (
    <Tip content={doc.error_message}>
      <span tabIndex={0} className={TIP_WRAP_FOCUS_RING} data-testid="knowledge-document-error">
        {badge}
      </span>
    </Tip>
  );
}

export default function KnowledgeDocuments() {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const reprocessDocument = useReprocessDocument();
  const aiOff = useAiActionDisabled();

  // Older links (`?status=pending,processing&page=2`) become the table's keys once.
  useEffect(() => {
    if (!searchParams.has("status") && !searchParams.has("page") && !searchParams.has("kb")) return;
    setSearchParams(
      (previous) => {
        const out = new URLSearchParams(previous);
        const legacy = (out.get("status") ?? "").split(",").filter((value) => (STATUSES as readonly string[]).includes(value));
        const search = out.get("kb") ?? "";
        out.delete("status");
        out.delete("page");
        out.delete("kb");
        const current = readTableState(out, URL_KEY, DEFAULTS);
        return writeTableState(
          out,
          URL_KEY,
          { ...current, search: current.search || search, filters: legacy.length ? { ...current.filters, status: legacy } : current.filters },
          DEFAULTS,
        );
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams]);

  const state = readTableState(searchParams, URL_KEY, DEFAULTS);
  const kbIds = searchParams.get(SCOPE_PARAM) ?? "";
  const scopedCount = kbIds.split(",").filter((value) => value.trim()).length;
  const sortKey = (SORT_KEYS as readonly string[]).includes(state.sort?.key ?? "") ? (state.sort?.key as DocumentSort) : "created_at";
  const statuses = (state.filters.status ?? []).filter((value) => (STATUSES as readonly string[]).includes(value));

  const { data, isLoading, isError, refetch } = useGlobalKnowledgeDocuments({
    limit: state.pageSize,
    offset: state.page * state.pageSize,
    status: statuses.length ? statuses.join(",") : "all",
    kbIds,
    q: state.search.trim() || undefined,
    sort: sortKey,
    direction: state.sort?.direction ?? "desc",
  });

  // Every visible document, before the scope, the status filter and the search: the total
  // reads "n of N" when they narrow it (#12039).
  const { data: everything } = useGlobalKnowledgeDocuments({ limit: 1, offset: 0 });

  const clearScope = () => {
    setSearchParams(
      (previous) => {
        const out = new URLSearchParams(previous);
        out.delete(SCOPE_PARAM);
        return writeTableState(out, URL_KEY, { ...readTableState(out, URL_KEY, DEFAULTS), page: 0 }, DEFAULTS);
      },
      { replace: true },
    );
  };
  const clearAll = () => {
    setSearchParams(
      (previous) => {
        const out = new URLSearchParams(previous);
        out.delete(SCOPE_PARAM);
        const current = readTableState(out, URL_KEY, DEFAULTS);
        return writeTableState(out, URL_KEY, { ...current, search: "", filters: {}, page: 0 }, DEFAULTS);
      },
      { replace: true },
    );
  };

  const filters = useMemo<DataTableFilter<GlobalDocument>[]>(
    () => [{ key: "status", label: "Status", options: STATUSES.map((value) => ({ value, label: t_i18n(STATUS_LABELS[value]) })) }],
    [t_i18n],
  );

  // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
  const sortable = { sortValue: () => null };
  const columns = useMemo<DataTableColumn<GlobalDocument>[]>(
    () => [
      {
        key: "filename",
        header: "Document",
        className: "max-w-0",
        cell: (doc) => (
          <span className="flex min-w-0 items-center gap-2">
            <Icon name="file-text" size={16} className="shrink-0 text-default-secondary" />
            <TruncatedText text={doc.filename} className="font-medium" />
          </span>
        ),
        csv: (doc) => doc.filename,
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "knowledge_base",
        header: "Knowledge base",
        width: COLUMN_WIDTHS.knowledge_base,
        className: "max-w-0",
        // Plain text: the row opens the document's knowledge base (rowHref, #12070).
        cell: (doc) => (
          <span className="flex min-w-0 max-w-full items-center gap-1" data-testid="knowledge-document-kb">
            <Icon name="database" size={16} className="shrink-0 text-default-secondary" />
            <TruncatedText text={doc.knowledge_base_name} />
          </span>
        ),
        csv: (doc) => doc.knowledge_base_name,
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "source",
        header: "Source",
        width: COLUMN_WIDTHS.source,
        hideable: true,
        hideBelow: "3xl",
        className: "max-w-0 truncate text-default-secondary",
        cell: (doc) => doc.source_type,
        csv: (doc) => doc.source_type,
      },
      {
        key: "status",
        header: "Status",
        width: COLUMN_WIDTHS.status,
        className: "whitespace-nowrap",
        cell: (doc) => <StatusBadge doc={doc} />,
        csv: (doc) => doc.status,
        ...sortable,
        firstSort: "asc",
      },
      {
        key: "chunks",
        header: "Chunks",
        align: "right",
        width: COLUMN_WIDTHS.chunks,
        hideable: true,
        hideBelow: "4xl",
        className: "tabular-nums text-default-secondary",
        cell: (doc) => doc.chunk_count,
        csv: (doc) => doc.chunk_count,
      },
      {
        key: "created_at",
        header: "Created",
        width: COLUMN_WIDTHS.created_at,
        className: "whitespace-nowrap text-default-secondary",
        cell: (doc) => <DateTimeCell value={doc.created_at} />,
        csv: (doc) => doc.created_at,
        ...sortable,
        firstSort: "desc",
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const rowActions = (doc: GlobalDocument) => {
    if (doc.status === "processing") {
      return <Spinner size="sm" tone="inherit" className="text-accent" label={t_i18n("Processing")} />;
    }
    // The reprocess route refuses a package-installed KB.
    if ((doc.status !== "error" && doc.status !== "pending") || doc.knowledge_base_is_package_origin) return null;
    const label = doc.status === "error" ? t_i18n("Retry embedding") : t_i18n("Re-queue for embedding");
    const pending = reprocessDocument.isPending && reprocessDocument.variables?.docId === doc.id;
    return (
      <Tip content={label}>
        <IconButton
          variant="default"
          priority="tertiary"
          aria-label={label}
          icon={pending ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} /> : <Icon name="refresh-cw" size={16} />}
          className="text-default-secondary hover:text-accent"
          disabled={aiOff || pending}
          aria-busy={pending || undefined}
          onClick={() => reprocessDocument.mutate({ kbId: doc.knowledge_base_id, docId: doc.id })}
        />
      </Tip>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <BackButton to="/knowledge" label="Back to Knowledge Bases" className="mb-4" />
        <h1 className="text-3xl font-bold">{t_i18n("Knowledge Base Documents")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Global document status view across all visible knowledge bases.")}</p>
      </div>

      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(doc) => doc.id}
        rowHref={(doc) => `/knowledge/${doc.knowledge_base_id}/documents`}
        server={{ total: data?.total ?? 0 }}
        totalAll={everything?.total}
        urlKey={URL_KEY}
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        searchPlaceholder={t_i18n("Search by file or knowledge base...")}
        filters={filters}
        filterPills={
          scopedCount > 0 ? (
            <span className={filterPillClass(true)} data-testid="knowledge-documents-scope">
              {t_i18n("{count, plural, one {# knowledge base} other {# knowledge bases}}", { count: scopedCount })}
              <button
                type="button"
                aria-label={t_i18n("Remove filter")}
                className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
                onClick={clearScope}
              >
                <Icon name="x" size={16} />
              </button>
            </span>
          ) : undefined
        }
        externalFilterCount={scopedCount > 0 ? 1 : 0}
        onClearAll={clearAll}
        exportName="knowledge-documents"
        layout="fixed"
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        rowActions={rowActions}
        rowActionsWidth={64}
        emptyLabel={t_i18n("No documents yet. Upload files, paste text, or sync from a source.")}
        noResultsLabel={t_i18n("No documents found for the selected filters.")}
        rowTestId="knowledge-document-row"
      />
    </div>
  );
}
