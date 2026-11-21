// SPDX-License-Identifier: Apache-2.0

/**
 * The audit log list on the shared DataTable (standard v2, #9378 slice 6y): the admin
 * `/logs` page and the Activity tab of a user draw the same columns, toolbar and row
 * detail. Server mode: the API searches, filters, sorts and pages (`GET /audit-logs`
 * takes `sort`, `direction` and a list of values per filter). The state
 * lives in the URL keys `/logs` always had, under `prefix` (`audit_` on a user's tab).
 * Action, Entity and User (the admin list only) are filter pills of the table; the time
 * range is the page's own pill beside them, counted by Clear all.
 */
import { useCallback, useId, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateRangeFilter } from "@/components/ui/date-range-filter";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FilterPillPanel, FilterPillTrigger, isInsideFloatingPicker, useFilterPopover } from "@/components/ui/filter-pill";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useAuditLogActions, useAuditLogEntityTypes, useAuditLogs } from "@/hooks/api/useMiscApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";
import {
  AUDIT_LOG_DEFAULT_PAGE_SIZE,
  AUDIT_LOG_DEFAULT_SORT,
  clearAuditLogFilters,
  readAuditLogParams,
  writeAuditLogRange,
  writeAuditLogTable,
} from "@/lib/audit-log-table-state";
import { AUDIT_ACTION_COLORS, AUDIT_ACTION_ICONS, COPY_FEEDBACK_DELAY, auditActionLabel, auditEntityLabel } from "@/lib/constants";
import type { DataTableState } from "@/lib/data-table-state";
import { storedPageSize } from "@/lib/data-table-state";
import { type DateRange, hasDateRange, resolveDateRange } from "@/lib/date-range";
import { formatDateTime } from "@/lib/utils";
import type { AuditLog } from "@/types";

/**
 * Column widths under `layout="fixed"`: the summary takes what is left, about 280 px on
 * the admin list at a 1080 px window (UX r1 B3 on #11939).
 */
const WIDTHS = { time: 160, action: 150, entity: 140, user: 180, ip: 140 } as const;

interface AuditLogListProps {
  /** The prefix of the table's URL keys: "" on `/logs`, `audit_` on a user's tab. */
  prefix: string;
  /** Per-user key of the page size and the hidden columns. */
  storageKey: string;
  /** A user's tab: the list is that user's, with no User column or filter. */
  userId?: string;
  /** Already translated: nothing logged yet. */
  emptyLabel: string;
}

/** The time range pill of the toolbar: a preset or a From / To day, as the page always had. */
function TimeRangePill({ value, onChange }: { value: DateRange; onChange: (next: DateRange) => void }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  useFilterPopover(rootRef, open, setOpen, isInsideFloatingPicker);
  return (
    <div ref={rootRef} className="relative shrink-0" data-testid="audit-logs-time-range">
      <FilterPillTrigger
        label={t_i18n("Time range")}
        open={open}
        count={hasDateRange(value) ? 1 : 0}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((v) => !v)}
      />
      {open && (
        <FilterPillPanel id={panelId} role="group" aria-label={t_i18n("Time range")} className="w-auto p-3">
          <DateRangeFilter value={value} onChange={onChange} stacked />
        </FilterPillPanel>
      )}
    </div>
  );
}

/** The row's detail: its summary, badges and the raw event with Copy. */
function AuditLogDialog({ log, onClose, userKnown }: { log: AuditLog | null; onClose: () => void; userKnown: boolean }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    if (!log) return;
    navigator.clipboard.writeText(JSON.stringify(log, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  }, [log]);
  return (
    <Dialog open={!!log} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col" onOpenAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <Icon name="file-text" size={16} className="text-accent" aria-hidden />
            {t_i18n("Audit Log Event")}
          </DialogTitle>
        </DialogHeader>
        {log && (
          <div className="flex-1 overflow-hidden flex flex-col gap-4" data-testid="audit-log-detail">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge variant="outline" className="content-caption">{auditActionLabel(log.action, t_i18n)}</Badge>
              <Badge variant="outline" className="content-caption">{auditEntityLabel(log.entity_type, t_i18n)}</Badge>
              {/* The user moved out of the table cell (#12070): the detail opens them. */}
              {userKnown ? (
                <Link to={`/users-and-groups/users/${log.user_id}`} className="content-compact text-accent hover:underline" data-testid="audit-log-detail-user">
                  {log.user_email}
                </Link>
              ) : (
                <span className="content-compact text-default-secondary">{log.user_email}</span>
              )}
              <span className="content-compact text-default-secondary">·</span>
              <span className="content-compact text-default-secondary">{formatDateTime(log.created_at)}</span>
            </div>
            <p className="content-base">{log.summary}</p>
            <div className="flex items-center justify-between gap-2">
              <p className="content-compact font-medium text-default-secondary">{t_i18n("Raw Event (JSON)")}</p>
              <Button variant="ghost" size="sm" className="h-6 content-compact gap-1 shrink-0" onClick={copy}>
                <Icon name={copied ? "check" : "copy"} size={16} className={copied ? "text-feedback-success-primary" : undefined} aria-hidden />
                {copied ? t_i18n("Copied") : t_i18n("Copy")}
              </Button>
            </div>
            <div className="flex-1 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-4">
              <pre className="content-compact text-default-secondary whitespace-pre-wrap break-words font-mono-plex">
                {JSON.stringify(log, null, 2)}
              </pre>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function AuditLogList({ prefix, storageKey, userId, emptyLabel }: AuditLogListProps) {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<AuditLog | null>(null);

  const fallbackSize = storedPageSize(storageKey, AUDIT_LOG_DEFAULT_PAGE_SIZE);
  const parsed = useMemo(() => readAuditLogParams(searchParams, prefix, fallbackSize), [searchParams, prefix, fallbackSize]);
  const { table, range } = parsed;
  const debouncedSearch = useDebounce(table.search, 300);
  // Resolved once per URL: a preset counts back from the moment it was picked.
  const dates = useMemo(() => resolveDateRange(parsed.range), [parsed.range]);

  const { data, isLoading, isError, refetch } = useAuditLogs({
    search: debouncedSearch || undefined,
    user_id: userId ?? ((table.filters.user ?? []).join(",") || undefined),
    action: (table.filters.action ?? []).join(",") || undefined,
    entity_type: (table.filters.entity ?? []).join(",") || undefined,
    sort: table.sort?.key,
    direction: table.sort?.direction,
    ...dates,
    limit: table.pageSize,
    offset: table.page * table.pageSize,
  });
  // The list before any search, filter or time range (the user's own on their tab): the
  // total then reads "n of N" when the toolbar narrows it (dev-docs data-tables, #12039).
  const { data: everything } = useAuditLogs({ user_id: userId, limit: 1, offset: 0 });
  const { data: actions } = useAuditLogActions();
  const { data: entityTypes } = useAuditLogEntityTypes();
  const { data: users } = useUsers(!userId);

  const onTableState = (next: DataTableState) =>
    setSearchParams((prev) => writeAuditLogTable(prev, prefix, table, next, AUDIT_LOG_DEFAULT_PAGE_SIZE), { replace: true });
  const onRange = (next: DateRange) => setSearchParams((prev) => writeAuditLogRange(prev, prefix, next), { replace: true });
  const clearAll = () => setSearchParams((prev) => clearAuditLogFilters(prev, prefix), { replace: true });

  const filters: DataTableFilter<AuditLog>[] = [
    { key: "action", label: "Action", options: (actions ?? []).map((a) => ({ value: a, label: auditActionLabel(a, t_i18n) })) },
    {
      key: "entity",
      label: "Entity",
      options: (entityTypes ?? []).map((e) => ({ value: e, label: auditEntityLabel(e, t_i18n) })),
    },
    ...(userId
      ? []
      : [{ key: "user", label: "User", options: (users ?? []).map((u) => ({ value: u.id, label: u.full_name?.trim() || u.email })) }]),
  ];

  const columns: DataTableColumn<AuditLog>[] = [
    {
      key: "time",
      // "Date", not "Time": the fr catalog reads "Time" as a duration (Durée).
      header: "Date",
      width: WIDTHS.time,
      firstSort: "desc",
      sortValue: (log) => log.created_at,
      cell: (log) => <DateTimeCell value={log.created_at} />,
    },
    {
      key: "summary",
      header: "Summary",
      cell: (log) => <TruncatedText text={log.summary} />,
    },
    {
      key: "action",
      header: "Action",
      width: WIDTHS.action,
      hideable: true,
      sortValue: (log) => log.action,
      cell: (log) => {
        const ActionIcon = AUDIT_ACTION_ICONS[log.action];
        return (
          <span className="flex min-w-0 items-center gap-1.5">
            {ActionIcon && <ActionIcon className={`size-3.5 shrink-0 ${AUDIT_ACTION_COLORS[log.action] ?? "text-default-secondary"}`} aria-hidden />}
            <Badge variant="outline" className="content-caption min-w-0 max-w-full">
              <TruncatedText text={auditActionLabel(log.action, t_i18n)} />
            </Badge>
          </span>
        );
      },
    },
    {
      key: "entity",
      header: "Entity",
      width: WIDTHS.entity,
      hideable: true,
      sortValue: (log) => log.entity_type,
      cell: (log) => (
        <Badge variant="outline" className="content-caption min-w-0 max-w-full">
          <TruncatedText text={auditEntityLabel(log.entity_type, t_i18n)} />
        </Badge>
      ),
    },
    ...(userId
      ? []
      : [
          {
            key: "user",
            header: "User",
            width: WIDTHS.user,
            hideable: true,
            sortValue: (log: AuditLog) => log.user_email.toLowerCase(),
            // Plain text: the whole row opens the log (#12070); the detail drawer reaches the user.
            cell: (log: AuditLog) => <TruncatedText text={log.user_email} />,
          },
        ]),
    {
      key: "ip",
      header: "IP address",
      width: WIDTHS.ip,
      hideable: true,
      defaultHidden: true,
      cell: (log) => <span className="content-compact text-default-secondary">{log.ip_address ?? "-"}</span>,
    },
  ];

  const rangeCount = hasDateRange(range) ? 1 : 0;

  return (
    <>
      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(log) => log.id}
        server={{ total: data?.total ?? 0 }}
        totalAll={everything?.total}
        state={table}
        onStateChange={onTableState}
        storageKey={storageKey}
        defaultPageSize={25}
        defaultSort={AUDIT_LOG_DEFAULT_SORT}
        searchPlaceholder={t_i18n("Search logs...")}
        compactSearch
        filters={filters}
        filterPills={<TimeRangePill value={range} onChange={onRange} />}
        externalFilterCount={rangeCount}
        onClearAll={clearAll}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={emptyLabel}
        noResultsLabel={t_i18n("No logs match your filters.")}
        onRowClick={setSelected}
        rowTestId="audit-log-row"
        layout="fixed"
      />
      <AuditLogDialog
        log={selected}
        onClose={() => setSelected(null)}
        userKnown={Boolean(selected && users?.some((u) => u.id === selected.user_id))}
      />
    </>
  );
}
