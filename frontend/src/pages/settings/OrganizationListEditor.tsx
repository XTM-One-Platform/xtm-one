// SPDX-License-Identifier: Apache-2.0

/**
 * The editor of one list of the organization profile while its card is in edit (#12256).
 *
 * The list keeps what read mode has (search, filters, sort, the pager) and never draws a wall
 * of inputs: every row reads as text, one row at a time turns into its fields (a click on the
 * row, or Enter on the focused row); Enter or Done closes it, Esc puts back the values it had
 * when opened. The order is the one the edit started with (sort and filters read each row's
 * value at that time, `base`), so a row never moves or disappears while typed in; a click on a
 * column header sorts again on what is typed. An added row is pinned first, open, focused.
 * A removed row stays struck through with Undo until Save; the Status filter shows the added,
 * edited, removed rows and the ones that need attention.
 *
 * Slice 2: rows are selected with their box and the toolbar sets one field on every selected
 * row (vendor, owner, criticality, kind, exposure) or removes them, each change marked and
 * undoable like a hand edit until Save.
 *
 * #1134: a row a platform sync owns is read-only but for its local enrichment fields
 * (`locked` names, per column, what the open row keeps as text, with a lock and the sync
 * named on its first cell); removing such rows asks first (`confirmRemove`): they come back
 * at the next run unless excluded from their sync.
 */
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter, type SortValue } from "@/components/ui/data-table";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { initialTableState, type DataTableSort, type DataTableState } from "@/lib/data-table-state";
import {
  changeCounts,
  type EditRow,
  type ListIssue,
  patchRows,
  removeRow,
  removeRows,
  rowSearchFields,
  rowState,
  setRowValue,
  undoRemove,
} from "@/lib/organization-list-edit";
import type { SortDirection } from "@/lib/table-rows";

export interface ListEditColumn<V> {
  key: string;
  header: string;
  width?: number;
  hideable?: boolean;
  /** The DataTable container width below which the column steps aside (still sorted and pickable). */
  hideBelow?: DataTableColumn<unknown>["hideBelow"];
  headerClassName?: string;
  /** The cell of a closed row. */
  read: (value: V) => ReactNode;
  /** The field of the open row; omitted = the column is never edited here (an OpenCRQ link). */
  edit?: (value: V, patch: (patch: Partial<V>) => void, invalid: boolean) => ReactNode;
  sortValue?: (value: V) => SortValue;
  firstSort?: SortDirection;
  /** The issues of a row this column's field carries (aria-invalid on its field). */
  issues?: readonly ListIssue[];
}

/** A field the toolbar sets on every selected row (slice 2). */
export interface BulkField<V> {
  key: keyof V & string;
  /** English label, translated here. */
  label: string;
  /** The choices of a select field (value to English label); omitted = free text. */
  options?: Record<string, string>;
  /** The id of a datalist suggesting the values of a free-text field. */
  suggestions?: string;
}

/** The page size an edit opens with (the read tables' default size is the user's own). */
const LIST_EDIT_PAGE_SIZE = 25;

/** The Status filter of an edit: what changed, and what needs attention. @i18n-keys */
const STATUS_LABELS = {
  attention: "Needs attention",
  new: "Added",
  edited: "Edited",
  removed: "Removed",
} as const;

export function OrganizationListEditor<V>({
  rows,
  onRows,
  columns,
  filters = [],
  searchFields,
  issues,
  openKey,
  onOpen,
  rowLabel,
  defaultSort,
  storageKey,
  rowTestId,
  emptyLabel,
  bulkFields = [],
  locked,
  confirmRemove,
}: {
  rows: EditRow<V>[];
  onRows: (rows: EditRow<V>[]) => void;
  columns: ListEditColumn<V>[];
  filters?: DataTableFilter<V>[];
  searchFields: (value: V) => readonly (string | null | undefined)[];
  issues: Map<string, ListIssue[]>;
  openKey: string | null;
  onOpen: (key: string | null) => void;
  /** The row's name, for the labels of its buttons ("Edit Jira"). */
  rowLabel: (value: V) => string;
  defaultSort?: DataTableSort;
  storageKey: string;
  rowTestId?: string;
  emptyLabel: string;
  bulkFields?: BulkField<V>[];
  /** The lock of a synced row (#1134): its tip, and whether a column stays text when the row is open; omitted = every row is editable. */
  locked?: { tip: (value: V) => string | null; column: (value: V, key: string) => boolean };
  /** Asked before synced rows are removed (#1134): false keeps them. */
  confirmRemove?: (values: V[]) => Promise<boolean>;
}) {
  const { t_i18n } = useFormatter();
  const box = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<DataTableState>(() => initialTableState({ sort: defaultSort ?? null, pageSize: LIST_EDIT_PAGE_SIZE }));
  // The values of the open row when it was opened: Esc puts them back.
  const opened = useRef<{ key: string; value: V } | null>(null);
  const byKey = useMemo(() => new Map(rows.map((row) => [row.key, row])), [rows]);

  const open = (key: string | null) => {
    const row = key ? byKey.get(key) : undefined;
    opened.current = row ? { key: row.key, value: row.value } : null;
    onOpen(row ? row.key : null);
  };
  // A row added from the card header opens with nothing to put back but its empty values.
  if (openKey && opened.current?.key !== openKey) {
    const row = byKey.get(openKey);
    opened.current = row ? { key: row.key, value: row.value } : null;
  }

  // The open row's first field takes the focus; a closed row gives it back to the row.
  const lastOpen = useRef<string | null>(null);
  useEffect(() => {
    const root = box.current;
    if (!root) return;
    if (openKey) {
      // An added row is first on the first page: the page jumps to it when it opens, never on a
      // later page change, so Next still pages while the row is open (UX r1 B3 on #12261).
      const opening = openKey !== lastOpen.current;
      if (opening && byKey.get(openKey)?.original === null && state.page !== 0) setState((prev) => ({ ...prev, page: 0 }));
      const line = root.querySelector(`[data-edit-key="${openKey}"]`)?.closest("tr");
      line?.querySelector<HTMLElement>("input, textarea, button[role='combobox']")?.focus();
    } else if (lastOpen.current) {
      root.querySelector(`[data-edit-key="${lastOpen.current}"]`)?.closest<HTMLElement>("tr")?.focus();
    }
    lastOpen.current = openKey;
    // Only an open or a close moves the focus, never a keystroke in the row.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openKey, state.page]);

  const close = () => open(null);
  const revert = () => {
    const at = opened.current;
    if (at) onRows(setRowValue(rows, at.key, at.value));
    open(null);
  };
  const onFieldKey = (event: React.KeyboardEvent) => {
    // A select or a picker that is open handles its own keys.
    if (event.defaultPrevented) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      revert();
    } else if (event.key === "Enter" && (event.target as HTMLElement).tagName === "INPUT") {
      event.preventDefault();
      close();
    }
  };

  const patch = (key: string) => (change: Partial<V>) =>
    onRows(rows.map((row) => (row.key === key ? { ...row, value: { ...row.value, ...change } } : row)));

  const tableColumns: DataTableColumn<EditRow<V>>[] = columns.map((column) => ({
    key: column.key,
    header: column.header,
    width: column.width,
    hideable: column.hideable,
    hideBelow: column.hideBelow,
    headerClassName: column.headerClassName,
    firstSort: column.firstSort,
    sortValue: column.sortValue ? (row: EditRow<V>) => column.sortValue!(row.base) : undefined,
    cell: (row: EditRow<V>) => {
      // A closed row's cells let the press through to the row, which opens it.
      if (row.key !== openKey || !column.edit || locked?.column(row.value, column.key)) return column.read(row.value);
      const rowIssues = issues.get(row.key) ?? [];
      const invalid = (column.issues ?? []).some((issue) => rowIssues.includes(issue));
      return (
        <div className="w-full" onKeyDown={onFieldKey} onClick={(event) => event.stopPropagation()}>
          {column.edit(row.value, patch(row.key), invalid)}
        </div>
      );
    },
  }));
  // The row's first cell names what it needs, in read and edit, as a mark with its reasons.
  const first = tableColumns[0];
  tableColumns[0] = {
    ...first,
    cell: (row) => {
      const reasons = row.removed ? [] : (issues.get(row.key) ?? []);
      const status = rowState(row);
      const lockTip = locked?.tip(row.value) ?? null;
      return (
        <div className="flex min-w-0 items-center gap-1.5" data-edit-key={row.key} data-open={row.key === openKey ? "true" : undefined}>
          {status !== "unchanged" && (
            <span
              data-testid="settings.organization.list-row-state"
              data-state={status}
              className={status === "removed" ? "content-compact text-feedback-error-primary" : "content-compact text-feedback-success-primary"}
            >
              {t_i18n(STATUS_LABELS[status])}
            </span>
          )}
          {reasons.length > 0 && (
            <Tip content={reasons.map((reason) => t_i18n(reason)).join(" ")}>
              <span role="img" aria-label={reasons.map((reason) => t_i18n(reason)).join(" ")} className="inline-flex shrink-0">
                <Icon name="triangle-alert" size={16} className="text-feedback-warning-primary" />
              </span>
            </Tip>
          )}
          {lockTip && (
            <Tip content={lockTip}>
              <span role="img" aria-label={lockTip} className="inline-flex shrink-0" data-testid="settings.organization.list-row-locked">
                <Icon name="lock" size={16} className="text-default-secondary" />
              </span>
            </Tip>
          )}
          <div className="min-w-0 flex-1">{first.cell(row)}</div>
        </div>
      );
    },
  };

  const statusFilter: DataTableFilter<EditRow<V>> = {
    key: "status",
    label: "Status",
    values: (row) => [rowState(row), ...(issues.has(row.key) ? ["attention"] : [])],
    options: Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
  };
  const rowFilters: DataTableFilter<EditRow<V>>[] = [
    statusFilter,
    // A row keeps the filter values of the edit's start too, so a filter never drops it while typed in.
    ...filters.map((filter) => ({
      ...filter,
      values: filter.values ? (row: EditRow<V>) => [...filter.values!(row.base), ...filter.values!(row.value)] : undefined,
    })),
  ];

  const onStateChange = (next: DataTableState) => {
    // A new sort is asked for: the rows sort again on what is typed now.
    if (JSON.stringify(next.sort) !== JSON.stringify(state.sort)) onRows(rows.map((row) => (row.base === row.value ? row : { ...row, base: row.value })));
    setState(next);
  };

  // The selected rows that still exist and are not removed (a removed or dropped row leaves the selection).
  const [picked, setPicked] = useState<string[]>([]);
  const selected = picked.filter((key) => byKey.has(key) && !byKey.get(key)!.removed);
  // Synced rows in what goes: asked first (#1134); anything else is removed at once (struck through, Undo until Save).
  const withConfirm = (keys: string[], remove: () => void) => {
    const values = keys.flatMap((key) => (byKey.get(key) ? [byKey.get(key)!.value] : []));
    if (!confirmRemove || !values.some((value) => locked?.tip(value))) return remove();
    void confirmRemove(values).then((confirmed) => confirmed && remove());
  };
  const bulkRemove = () =>
    withConfirm(selected, () => {
      if (openKey && selected.includes(openKey)) open(null);
      onRows(removeRows(rows, selected));
      setPicked([]);
    });

  const counts = changeCounts(rows);
  return (
    <div ref={box} data-testid="settings.organization.list-editor" data-changes={counts.added + counts.edited + counts.removed}>
      <DataTable
        rows={rows}
        columns={tableColumns}
        rowKey={(row) => row.key}
        state={state}
        onStateChange={onStateChange}
        searchFields={(row) => rowSearchFields(row, searchFields)}
        searchPlaceholder={t_i18n("Search...")}
        filters={rowFilters}
        defaultSort={defaultSort}
        defaultPageSize={LIST_EDIT_PAGE_SIZE}
        storageKey={`${storageKey}-edit`}
        layout="fixed"
        emptyLabel={emptyLabel}
        rowTestId={rowTestId}
        selection={{
          selected,
          onChange: setPicked,
          disabledReason: (key) => (byKey.get(key)?.removed ? t_i18n("Removed: undo it to select it.") : undefined),
          actions: (
            <>
              {bulkFields.length > 0 && <BulkSet fields={bulkFields} onApply={(patch) => onRows(patchRows(rows, selected, patch as Partial<V>, locked?.column))} />}
              <Button variant="secondary" className="gap-1.5" onClick={bulkRemove} data-testid="settings.organization.bulk-remove">
                <Icon name="trash-2" size={16} aria-hidden /> {t_i18n("Remove")}
              </Button>
            </>
          ),
        }}
        pinFirst={(row) => row.original === null}
        rowClassName={(row) => (row.removed ? "line-through text-default-secondary" : row.key === openKey ? "bg-elevation-hover" : undefined)}
        onRowClick={(row) => open(row.key)}
        rowOpens={(row) => !row.removed && row.key !== openKey}
        rowActions={(row) =>
          row.removed ? (
            <Button variant="ghost" className="gap-1.5 no-underline" onClick={() => onRows(undoRemove(rows, row.key))} aria-label={t_i18n("Undo the removal of {name}", { name: rowLabel(row.value) || t_i18n("this row") })}>
              <Icon name="rotate-ccw" size={16} aria-hidden /> {t_i18n("Undo")}
            </Button>
          ) : (
            <>
              {row.key === openKey && (
                <Button variant="ghost" onClick={close}>
                  {t_i18n("Done")}
                </Button>
              )}
              <RowRemoveButton
                label={t_i18n("Remove")}
                onClick={() =>
                  withConfirm([row.key], () => {
                    if (row.key === openKey) open(null);
                    onRows(removeRow(rows, row.key));
                  })
                }
              />
            </>
          )
        }
      />
    </div>
  );
}

/**
 * The bulk "Set" of the selection toolbar: a field, its value (its choices, or free text with
 * the list's suggestions), Apply. The value is kept per field, so switching back finds it.
 */
function BulkSet<V>({ fields, onApply }: { fields: BulkField<V>[]; onApply: (patch: Record<string, string>) => void }) {
  const { t_i18n } = useFormatter();
  const [field, setField] = useState(fields[0].key as string);
  const [values, setValues] = useState<Record<string, string>>({});
  const current = fields.find((item) => item.key === field) ?? fields[0];
  const value = values[current.key] ?? "";
  const setValue = (next: string) => setValues((prev) => ({ ...prev, [current.key]: next }));
  return (
    <div className="flex items-center gap-2" data-testid="settings.organization.bulk-set">
      <Select value={current.key} onValueChange={setField}>
        <SelectTrigger className="w-40" aria-label={t_i18n("Field to set")}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {fields.map((item) => (
            <SelectItem key={item.key} value={item.key}>
              {t_i18n(item.label)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {current.options ? (
        <Select value={value} onValueChange={setValue}>
          <SelectTrigger className="w-40" aria-label={t_i18n("Value of {field}", { field: t_i18n(current.label) })}>
            <SelectValue placeholder={t_i18n("Choose a value")} />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(current.options).map(([key, label]) => (
              <SelectItem key={key} value={key}>
                {t_i18n(label)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <Input
          className="w-48"
          aria-label={t_i18n("Value of {field}", { field: t_i18n(current.label) })}
          placeholder={t_i18n(current.label)}
          list={current.suggestions}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
      )}
      <Button
        variant="secondary"
        disabled={current.options ? !value : false}
        onClick={() => onApply({ [current.key]: value.trim() })}
        data-testid="settings.organization.bulk-apply"
      >
        {t_i18n("Set on selected")}
      </Button>
    </div>
  );
}

/**
 * The Save / Cancel bar of one list card in edit, pinned to the bottom of the scroll area while
 * the card is taller than the window: it says what Save writes ("3 added, 2 edited, 1 removed"),
 * and Save waits for a change.
 */
export function ListSaveBar({
  counts,
  saving,
  onSave,
  onCancel,
}: {
  counts: { added: number; edited: number; removed: number };
  saving: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { t_i18n } = useFormatter();
  const parts = [
    counts.added ? t_i18n("{count, plural, one {# added} other {# added}}", { count: counts.added }) : "",
    counts.edited ? t_i18n("{count, plural, one {# edited} other {# edited}}", { count: counts.edited }) : "",
    counts.removed ? t_i18n("{count, plural, one {# removed} other {# removed}}", { count: counts.removed }) : "",
  ].filter(Boolean);
  return (
    <div
      data-testid="settings.organization.list-save-bar"
      className="sticky bottom-0 z-10 -mx-4 mt-3 flex items-center justify-end gap-3 border-t border-elevation-subtle bg-elevation-default px-4 py-3"
    >
      <span className="mr-auto content-compact text-default-secondary" role="status">
        {parts.length ? parts.join(", ") : t_i18n("No change yet")}
      </span>
      <Button type="button" variant="secondary" onClick={onCancel} disabled={saving}>
        {t_i18n("Cancel")}
      </Button>
      <Button type="button" className="gap-1.5" onClick={onSave} disabled={saving || parts.length === 0} aria-busy={saving || undefined}>
        <Icon name="check" size={16} aria-hidden />
        {t_i18n("Save")}
      </Button>
    </div>
  );
}
