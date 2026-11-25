// SPDX-License-Identifier: Apache-2.0

/**
 * Sources and syncs (#1276, replaces the Sources panel of #1232 and the Syncs card of #11913):
 * ONE table of who feeds the organization profile, used as is in Settings > Organization and
 * on the CTEM Inventory (scope "profile" / "inventory" / "" for every section).
 *
 * One row per sync (a platform reading or writing a section on a schedule) and one row per
 * source with no sync (an agent or a partner writing through the API, a CSV import, the API,
 * rows entered by hand): its kind in words, the sections it feeds with their row counts, the
 * direction, the filters, the schedule, the last run (counts or its error), the exact next run
 * (or why there is none) and the state. Row actions: Run now, Edit, Pause or Resume, View
 * history, Delete with the choice of what becomes of the rows it wrote (keep them owned by the
 * source, make them editable by hand, or purge them, with the dry-run counts); a source with no
 * sync offers Make editable and Purge for every kind but rows entered by hand (the backend's rule,
 * #1940), and a platform whose rows outlived their sync also offers Set up a sync.
 */
import { useState } from "react";
import { Alert, Chip, Icon, Radio, RadioGroup, Spinner } from "@filigran/design-system";
import { useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SettingsCard } from "@/components/ui/settings-form";
import { SlideOver } from "@/components/ui/slide-over";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  useApplyOrganizationSourceRows,
  useDeleteOrganizationSync,
  useOrganizationRowsPreview,
  useOrganizationSources,
  useOrganizationSyncHistory,
  useOrganizationSyncs,
  useRunOrganizationSync,
  useRunOrganizationSyncs,
  useUpdateOrganizationSync,
  type OrganizationSource,
  type ImportSectionKey,
  type ImportSourceKey,
  type OrganizationSync,
  type OrganizationSyncScope,
  type RowsPlan,
  type SyncRowsMode,
} from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { filtersSummary } from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES, IMPORT_SOURCE_PROVIDER } from "@/lib/organization-profile";
import { SOURCE_KIND_LABELS } from "@/lib/organization-provenance";
import { SOURCED_SECTION_LABELS, sourceSectionHref, type ChipSource, type SourcedSection } from "@/lib/organization-sources";
import { useScheduleLabel } from "@/lib/organization-sync-schedule";
import { formatCount } from "@/lib/number-format";
import { formatTimeAgo } from "@/lib/relative-time";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";
import { bodyOf, DIRECTION_LABELS, directionOf, stateOf, SYNC_SECTION_LABELS, SyncEditDialog, type SyncState } from "./OrganizationSyncEditDialog";

/** The kinds of source only this table names. @i18n-keys */
const SYNC_KIND_NAMES = {
  platform: "Platform sync",
  agent: "Agent or partner",
} as const;

/**
 * The kinds of source in words (#1276: never "Typed", never a raw slug). CSV, API and hand rows reuse the shared
 * SOURCE_KIND_LABELS, the names the Inventory lists and filters use (UX round 6 on #1254, B12).
 */
const SOURCE_KIND_NAMES = {
  ...SYNC_KIND_NAMES,
  csv: SOURCE_KIND_LABELS.csv,
  api: SOURCE_KIND_LABELS.api,
  manual: SOURCE_KIND_LABELS.manual,
} as const satisfies Record<OrganizationSource["kind"], string>;

/** The state of a row: a sync's own state, or "source" for a source with no sync. */
type RowState = SyncState | "failed" | "source";

/** What each kind of source with no sync waits for, in its own words (UX r1 B4). @i18n-keys */
const SOURCE_SCHEDULE_LABELS = {
  platform: "No sync scheduled",
  agent: "Writes when the agent sends rows",
  csv: "Writes at each CSV import",
  api: "Writes at each API call",
  manual: "Changes when a person edits the profile",
} as const satisfies Record<OrganizationSource["kind"], string>;

/** The rows a plan touches, per section, as ICU plurals (UX r1 B6). @i18n-keys */
const SECTION_COUNT_LABELS = {
  primary_assets: "{count, plural, one {# primary asset} other {# primary assets}}",
  software: "{count, plural, one {# software entry} other {# software entries}}",
  assets_exposure: "{count, plural, one {# asset} other {# assets}}",
  asset_groups: "{count, plural, one {# asset group} other {# asset groups}}",
} as const satisfies Record<SourcedSection, string>;

/** The kind of a history entry in words, never the API's raw value (UX r1 B7). @i18n-keys */
const HISTORY_KIND_LABELS = {
  schedule: "Scheduled run",
  run_now: "Run now",
  run: "Run",
  create: "Created",
  update: "Edited",
  delete: "Deleted",
  configure: "Settings changed",
  other: "Change",
} as const;

/** The kind of a history entry: how a run started, else what was saved. */
function historyKindOf(entry: { action: string; trigger: string }): keyof typeof HISTORY_KIND_LABELS {
  const trigger = entry.trigger.toLowerCase();
  if (trigger.startsWith("schedule")) return "schedule";
  if (trigger.startsWith("run now")) return "run_now";
  if (entry.action === "trigger") return "run";
  return entry.action in HISTORY_KIND_LABELS ? (entry.action as keyof typeof HISTORY_KIND_LABELS) : "other";
}

/** @i18n-keys */
const ROW_STATE_LABELS = {
  enabled: "Enabled",
  paused: "Paused",
  disabled: "Disabled",
  failed: "Failed",
  source: "No schedule",
} as const satisfies Record<RowState, string>;

/** The profile field a sync section fills (the API's rows are keyed by field). */
const SECTION_FIELD: Partial<Record<OrganizationSync["section"], SourcedSection>> = {
  assets: "assets_exposure",
  asset_groups: "asset_groups",
  software: "software",
  primary_assets: "primary_assets",
};

const isSourcedField = (field: string): field is SourcedSection => field in SOURCED_SECTION_LABELS;

/** One line of the table: a sync (with the source it reads), or a source no sync feeds. */
type Row = { id: string; sync: OrganizationSync; source: OrganizationSource | undefined } | { id: string; sync: null; source: OrganizationSource };

const rowStateOf = (row: Row): RowState => {
  if (!row.sync) return "source";
  if (row.sync.enabled && !row.sync.paused_reason && row.sync.last_result?.error) return "failed";
  return stateOf(row.sync);
};

/** The section of the import a profile field is fed by (the reverse of SECTION_FIELD). */
const FIELD_IMPORT_SECTION: Record<SourcedSection, ImportSectionKey> = {
  assets_exposure: "assets",
  asset_groups: "asset_groups",
  software: "software",
  primary_assets: "primary_assets",
};

/**
 * Every source but rows entered by hand can be purged or handed over (#1940): the backend's plan_rows refuses only
 * the hand source, and stays the one authority. A source with no row has nothing to change.
 */
const canPurgeSource = (source: OrganizationSource) => source.kind !== "manual" && source.total > 0;

/** What "Set up a sync" preselects in the import: the platform and the section its rows feed. */
export interface SyncPreset {
  section: ImportSectionKey;
  source: ImportSourceKey;
}

interface Props {
  /**
   * The sections listed: "profile" (primary assets) in Settings, "inventory" (assets, asset groups,
   * software inventory) on the CTEM Inventory, "" every section. A Sync all schedule saved before #1548 shows in each.
   */
  scope?: OrganizationSyncScope;
  /** Opens the step-by-step import (platform, section, filters with a live preview, schedule, review). */
  onAddSource?: () => void;
  /**
   * Opens the same import preselected on a platform and a section (#1940): offered as "Set up a sync" on a platform
   * source with no sync. Without it the action is not drawn.
   */
  onSetUpSync?: (preset: SyncPreset) => void;
  /** The link of a section chip, the source's key and display label given; `null` renders the chip as plain text. */
  sectionHref?: (field: SourcedSection, source: ChipSource) => string | null;
  /** Actions are off while the host saves (the Prioritization policy card being saved). */
  disabled?: boolean;
  /** Why the actions are off, said in a tooltip on Add a source. */
  disabledReason?: string;
  /**
   * "card": its own Settings card with the shared-context notice (CTEM > Inventory > Sources). "section": a block inside
   * a host card, titled by the host (the OpenCTI PIR sources on the Prioritization policy card); the import and
   * sync dialogs carry the shared-context notice.
   */
  variant?: "card" | "section";
  /** The section variant's heading and its one line. */
  title?: string;
  description?: string;
}

export function OrganizationSourcesSyncs({
  scope = "",
  onAddSource,
  onSetUpSync,
  sectionHref = sourceSectionHref,
  disabled = false,
  disabledReason,
  variant = "card",
  title,
  description,
}: Props) {
  const { t_i18n, locale, intl } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const scheduleLabel = useScheduleLabel();
  const syncsQuery = useOrganizationSyncs(scope);
  const sourcesQuery = useOrganizationSources(scope);
  const syncs = Array.isArray(syncsQuery.data) ? syncsQuery.data : [];
  const sources = Array.isArray(sourcesQuery.data) ? sourcesQuery.data : [];
  const { isAdmin } = useAuth();
  const update = useUpdateOrganizationSync();
  const run = useRunOrganizationSync();
  const runAll = useRunOrganizationSyncs();
  const [editing, setEditing] = useState<OrganizationSync | null>(null);
  const [deleting, setDeleting] = useState<Row | null>(null);
  const [history, setHistory] = useState<OrganizationSync | null>(null);

  const sourceOf = (key: string) => sources.find((source) => source.key === key);
  const rows: Row[] = [
    ...syncs.map((sync): Row => ({ id: `sync:${sync.id}`, sync, source: sync.source ? sourceOf(sync.source) : undefined })),
    ...sources.filter((source) => !syncs.some((sync) => sync.source === source.key && sync.section !== "all")).map((source): Row => ({ id: `source:${source.key}`, sync: null, source })),
  ];

  const reportFailure = (title: string) => (error: unknown) =>
    toast({ title, description: error instanceof Error ? error.message : undefined, variant: "error" });

  const exactTime = (iso: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));

  const nameOf = (row: Row): string => {
    if (row.sync?.section === "all") return t_i18n("Every section sync");
    if (row.sync) return row.source?.display_name ?? IMPORT_SOURCE_NAMES[row.sync.source] ?? row.sync.source;
    if (row.source.kind === "manual") return t_i18n(SOURCE_KIND_NAMES.manual);
    if (row.source.kind === "api") return t_i18n(SOURCE_KIND_NAMES.api);
    if (row.source.kind === "csv") return t_i18n(SOURCE_KIND_NAMES.csv);
    // An agent or a partner row is named by the source it writes for (Microsoft Intune), its
    // agent on the subline; the agent names the row only when the source has no name of its own.
    const named = row.source.display_name && row.source.display_name !== row.source.key;
    return named ? row.source.display_name : row.source.written_by_agent || row.source.display_name;
  };

  const kindOf = (row: Row): OrganizationSource["kind"] => (row.sync ? "platform" : row.source.kind);

  /** The schedule words of a source with no sync: a platform with rows kept those of a deleted sync (#1940). */
  const sourceScheduleText = (source: OrganizationSource): string =>
    source.kind === "platform" && source.total > 0 ? t_i18n("Rows from an earlier sync, none scheduled") : t_i18n(SOURCE_SCHEDULE_LABELS[source.kind]);

  /** The sections a row feeds, with their row counts (a sync: its own section). */
  const fieldsOf = (row: Row): { field: SourcedSection; count: number }[] => {
    const counts = row.source?.rows ?? {};
    if (row.sync) {
      const field = SECTION_FIELD[row.sync.section];
      return field ? [{ field, count: counts[field] ?? 0 }] : [];
    }
    return Object.entries(counts)
      .filter((entry): entry is [SourcedSection, number] => isSourcedField(entry[0]) && entry[1] > 0)
      .map(([field, count]) => ({ field, count }));
  };

  const filtersLine = (sync: OrganizationSync): string => {
    if (sync.section === "all") return t_i18n("In the order they were saved");
    return filtersSummary(t_i18n, locale, sync.filters) ?? t_i18n("No filter: every entity");
  };

  const lastRunLine = (sync: OrganizationSync): string => {
    if (!sync.last_run_at) return t_i18n("Never run");
    const when = formatTimeAgo(intl, sync.last_run_at);
    const result = sync.last_result ?? {};
    if (result.error) return t_i18n("{when}: failed", { when });
    return t_i18n("{when}: {added} added, {updated} updated, {removed} removed", {
      when,
      added: result.added ?? 0,
      updated: result.updated ?? 0,
      removed: result.removed ?? 0,
    });
  };

  const nextRunLine = (sync: OrganizationSync): string => {
    if (!sync.enabled) return t_i18n("Disabled: enable it to run again");
    if (sync.paused_reason) return t_i18n("Paused: {reason}", { reason: sync.paused_reason });
    return sync.next_run_at ? exactTime(sync.next_run_at) : t_i18n("Not scheduled");
  };

  const runNow = (sync: OrganizationSync) =>
    run.mutate(sync.id, {
      onSuccess: (done) =>
        done.last_result?.error
          ? toast({ title: t_i18n("The sync ran and failed"), description: done.last_result.error || done.paused_reason || undefined, variant: "error" })
          : toast({
              title: t_i18n("Sync run"),
              description: t_i18n("{added} added, {updated} updated, {removed} removed", {
                added: done.last_result?.added ?? 0,
                updated: done.last_result?.updated ?? 0,
                removed: done.last_result?.removed ?? 0,
              }),
              variant: "success",
            }),
      onError: reportFailure(t_i18n("The sync could not run")),
    });

  /**
   * Run all syncs (#1548): every enabled section sync of the card's scope runs now, one after the other, through Run
   * now; a saved Sync all row is left out, its section syncs run here already. One toast gives the count started.
   */
  const runnable = syncs.filter((sync) => sync.section !== "all" && sync.enabled && !sync.paused_reason);
  const runAllNow = () => {
    const ids = runnable.map((sync) => sync.id);
    if (ids.length === 0) return;
    toast({ title: t_i18n("{count, plural, one {# sync started} other {# syncs started}}", { count: ids.length }), variant: "success" });
    runAll.mutate(ids, {
      onSuccess: ({ failed }) => {
        if (failed > 0) toast({ title: t_i18n("{count, plural, one {# sync failed} other {# syncs failed}}", { count: failed }), variant: "error" });
      },
      onError: reportFailure(t_i18n("The syncs could not run")),
    });
  };

  const setEnabled = (sync: OrganizationSync, enabled: boolean) =>
    update.mutate({ id: sync.id, body: bodyOf(sync, { enabled }) }, { onError: reportFailure(t_i18n("The sync could not be saved")) });

  /** The import a platform source with no sync is set up from: its platform and the first section its rows feed. */
  const presetOf = (row: Row): SyncPreset | null => {
    if (row.sync || row.source.kind !== "platform" || !(row.source.key in IMPORT_SOURCE_NAMES)) return null;
    const [first] = fieldsOf(row);
    return first ? { section: FIELD_IMPORT_SECTION[first.field], source: row.source.key as ImportSourceKey } : null;
  };

  const menuOf = (row: Row): OverflowMenuItem[] => {
    const name = nameOf(row);
    if (row.sync) {
      const sync = row.sync;
      return [
        { id: "run", label: t_i18n("Run now"), icon: PlayIcon, onSelect: () => runNow(sync), disabled: disabled || run.isPending || runAll.isPending },
        { id: "edit", label: t_i18n("Edit"), icon: PencilIcon, onSelect: () => setEditing(sync), disabled },
        sync.enabled
          ? { id: "disable", label: t_i18n("Disable"), icon: PauseIcon, onSelect: () => setEnabled(sync, false), disabled: disabled || update.isPending }
          : { id: "enable", label: t_i18n("Enable"), icon: PlayIcon, onSelect: () => setEnabled(sync, true), disabled: disabled || update.isPending },
        { id: "history", label: t_i18n("View history"), icon: HistoryIcon, onSelect: () => setHistory(sync) },
        { id: "delete", label: t_i18n("Delete {name}", { name }), icon: TrashIcon, onSelect: () => setDeleting(row), destructive: true, separatorBefore: true, disabled },
      ];
    }
    const items: OverflowMenuItem[] = [];
    const preset = presetOf(row);
    if (preset && onSetUpSync) items.push({ id: "set-up-sync", label: t_i18n("Set up a sync"), icon: PlusIcon, onSelect: () => onSetUpSync(preset), disabled });
    if (canPurgeSource(row.source)) {
      items.push({
        id: "rows",
        label: t_i18n("Remove or hand over the rows"),
        icon: TrashIcon,
        onSelect: () => setDeleting(row),
        destructive: true,
        separatorBefore: items.length > 0,
        disabled,
      });
    }
    return items;
  };

  const columns: DataTableColumn<Row>[] = [
    {
      key: "source",
      header: "Source",
      cell: (row) => {
        const kind = kindOf(row);
        const platform = row.sync?.source || (kind === "platform" ? row.source?.key : "");
        return (
          <span className="flex min-w-0 items-center gap-2">
            {platform ? <ProviderLogo provider={IMPORT_SOURCE_PROVIDER[platform] ?? platform} decorative className="h-4 w-4 shrink-0" /> : <Icon name={kind === "agent" ? "bot" : kind === "csv" ? "file-text" : "user"} size={16} aria-hidden className="shrink-0 text-default-secondary" />}
            <span className="min-w-0">
              <TruncatedText text={nameOf(row)} className="max-w-[11rem] font-medium" />
              {kind === "agent" && row.source?.written_by_agent && (
                // One line, ellipsized with the whole text in the tooltip: a wrapped subline made agent rows taller in es and ja.
                <TruncatedText text={t_i18n("Written by agent {name}", { name: row.source.written_by_agent })} className="max-w-[11rem] content-compact text-default-secondary" />
              )}
            </span>
          </span>
        );
      },
      sortValue: (row) => nameOf(row),
      firstSort: "asc",
      csv: (row) => row.sync?.source ?? row.source?.key ?? "",
    },
    {
      key: "kind",
      header: "Kind",
      // The Kind filter and the source's icon keep the kind readable when the column steps aside.
      hideable: true,
      hideBelow: "4xl",
      // Wraps on two lines: a long translation (es "Agente o socio") never widens the table.
      cell: (row) => <span className="block max-w-[8rem]">{row.sync?.section === "all" ? t_i18n("Sync all") : t_i18n(SOURCE_KIND_NAMES[kindOf(row)])}</span>,
      sortValue: (row) => kindOf(row),
      csv: (row) => kindOf(row),
    },
    {
      key: "sections",
      header: "Sections fed",
      interactive: true,
      cell: (row) => {
        if (row.sync?.section === "all") return <span className="text-default-secondary">{t_i18n("Every enabled section sync")}</span>;
        const fields = fieldsOf(row);
        if (fields.length === 0) return <span className="text-default-secondary">{row.sync ? t_i18n(SYNC_SECTION_LABELS[row.sync.section]) : t_i18n("No row yet")}</span>;
        const name = nameOf(row);
        // The profile tables filter on the label of their Source column (the platform name, else the kind), the
        // Inventory's Assets list on the source key (UX round 5 on #1254, B11).
        const kind = kindOf(row);
        const chipSource: ChipSource = {
          key: row.sync?.source ?? row.source?.key ?? "",
          label: kind === "platform" ? name : t_i18n(SOURCE_KIND_LABELS[kind]),
        };
        return (
          <span className="flex flex-wrap gap-1.5">
            {fields.map(({ field, count }) => {
              const label = t_i18n(SOURCED_SECTION_LABELS[field]);
              const href = sectionHref(field, chipSource);
              return (
                <Chip
                  key={field}
                  label={`${label} ${formatCount(locale, count)}`}
                  onClick={href ? () => navigate(href) : undefined}
                  // A link names its target; plain text reads as drawn, the row already names the source.
                  aria-label={href ? t_i18n("{section} from {source}: {count, plural, one {# row} other {# rows}}", { section: label, source: name, count }) : undefined}
                  data-testid="organization-sources.chip"
                />
              );
            })}
          </span>
        );
      },
      sortValue: (row) => row.source?.total ?? 0,
      csv: (row) => fieldsOf(row).map(({ field, count }) => `${field}:${count}`).join(" "),
    },
    {
      key: "direction",
      header: "Direction",
      hideable: true,
      hideBelow: "4xl",
      cell: (row) =>
        !row.sync || row.sync.section === "all" ? null : (
          <span className="flex items-center gap-1.5 whitespace-nowrap">
            <Icon name={directionOf(row.sync) === "outbound" ? "arrow-up-right" : "arrow-down-left"} size={16} aria-hidden className="shrink-0 text-default-secondary" />
            {t_i18n(DIRECTION_LABELS[directionOf(row.sync)])}
          </span>
        ),
      sortValue: (row) => (row.sync && row.sync.section !== "all" ? directionOf(row.sync) : ""),
      csv: (row) => (row.sync && row.sync.section !== "all" ? directionOf(row.sync) : ""),
    },
    {
      key: "filters",
      header: "Filters",
      hideable: true,
      // Behind Columns by default: at 1728 the fr, es and ja headers pushed Actions out of the
      // 1458 px card (UX lab retest of #1294). Still searched and exported.
      defaultHidden: true,
      cell: (row) => (row.sync ? <TruncatedText text={filtersLine(row.sync)} lines={2} className="w-max max-w-[14rem] content-compact text-default-secondary" /> : null),
      csv: (row) => (row.sync ? filtersLine(row.sync) : ""),
    },
    {
      key: "schedule",
      header: "Schedule",
      hideable: true,
      hideBelow: "6xl",
      cell: (row) =>
        row.sync ? (
          <TruncatedText text={scheduleLabel(row.sync)} lines={2} className="w-max max-w-[12rem]" />
        ) : (
          <span className="text-default-secondary">{sourceScheduleText(row.source)}</span>
        ),
      sortValue: (row) => row.sync?.frequency ?? "",
      csv: (row) => (row.sync ? scheduleLabel(row.sync) : sourceScheduleText(row.source)),
    },
    {
      key: "last_run",
      header: "Last run",
      hideable: true,
      hideBelow: "4xl",
      cell: (row) => {
        if (!row.sync) return null;
        const sync = row.sync;
        return sync.last_result?.error ? (
          <Tip content={sync.last_result.error}>
            <span className="block w-max max-w-[12rem] line-clamp-2 text-feedback-error-primary" tabIndex={0} data-testid="organization-sources.last-error">
              {lastRunLine(sync)}
            </span>
          </Tip>
        ) : (
          <TruncatedText text={lastRunLine(sync)} lines={2} className="w-max max-w-[12rem] text-default-secondary" />
        );
      },
      sortValue: (row) => row.sync?.last_run_at ?? "",
      csv: (row) => row.sync?.last_run_at ?? "",
    },
    {
      key: "next_run",
      header: "Next run",
      hideable: true,
      // Clamped so the default columns fit an 810 px card; the whole reason is in the tooltip.
      cell: (row) => (row.sync ? <TruncatedText text={nextRunLine(row.sync)} lines={2} className="w-max max-w-[8rem] text-default-secondary" /> : null),
      sortValue: (row) => row.sync?.next_run_at ?? "",
      csv: (row) => row.sync?.next_run_at ?? "",
    },
    {
      key: "state",
      header: "State",
      cell: (row) => {
        const state = rowStateOf(row);
        const variant = state === "failed" ? "destructive" : state === "paused" ? "warning" : "outline";
        return (
          <Badge variant={variant} className="whitespace-nowrap">
            {t_i18n(ROW_STATE_LABELS[state])}
          </Badge>
        );
      },
      sortValue: (row) => rowStateOf(row),
      csv: (row) => rowStateOf(row),
    },
  ];

  const filters: DataTableFilter<Row>[] = [
    {
      key: "kind",
      label: "Kind",
      values: (row) => [kindOf(row)],
      options: (Object.keys(SOURCE_KIND_NAMES) as OrganizationSource["kind"][]).map((kind) => ({ value: kind, label: t_i18n(SOURCE_KIND_NAMES[kind]) })),
    },
    {
      key: "state",
      label: "State",
      values: (row) => [rowStateOf(row)],
      options: (Object.keys(ROW_STATE_LABELS) as RowState[]).map((state) => ({ value: state, label: t_i18n(ROW_STATE_LABELS[state]) })),
    },
  ];

  const rowActions = (row: Row) => {
    const items = menuOf(row);
    const name = nameOf(row);
    return (
      <span className="inline-flex items-center gap-1">
        {row.sync && (
          <Tip content={t_i18n("Run now")}>
            <Button variant="ghost" size="icon" aria-label={t_i18n("Run {name} now", { name })} onClick={() => row.sync && runNow(row.sync)} disabled={disabled || run.isPending || runAll.isPending}>
              {run.isPending && run.variables === row.sync.id ? <Spinner size="sm" tone="inherit" label={t_i18n("Running...")} /> : <Icon name="play" size={16} aria-hidden />}
            </Button>
          </Tip>
        )}
        {items.length > 0 && <OverflowMenu items={items} label={t_i18n("Actions on {name}", { name })} className="h-8 w-8" />}
      </span>
    );
  };

  // The summary line: what feeds the page, when it next runs, what failed.
  const sectionSyncs = syncs.filter((sync) => sync.section !== "all");
  const failed = rows.filter((row) => rowStateOf(row) === "failed");
  const nextRuns = syncs
    .filter((sync) => sync.enabled && !sync.paused_reason && sync.next_run_at)
    .map((sync) => sync.next_run_at as string)
    .sort();
  const summary = [
    // Distinct sources: a platform with several syncs counts once (UX r1 B3).
    t_i18n("{count, plural, one {# source} other {# sources}}", {
      count: new Set(rows.filter((row) => row.sync?.section !== "all").map((row) => (row.sync ? row.sync.source : row.source.key))).size,
    }),
    t_i18n("{count, plural, one {# sync} other {# syncs}}", { count: sectionSyncs.length }),
    nextRuns.length > 0 ? t_i18n("next run {time}", { time: exactTime(nextRuns[0]) }) : t_i18n("no run scheduled"),
  ].join(", ");

  const addSource = onAddSource && (
    <Tip content={disabled && disabledReason ? disabledReason : ""} wrap="auto">
      <Button
        variant={variant === "section" ? "secondary" : undefined}
        className="gap-1.5"
        onClick={onAddSource}
        disabled={disabled}
        data-testid="organization-sources.add"
      >
        <Icon name="plus" size={16} aria-hidden /> {t_i18n("Add a sync")}
      </Button>
    </Tip>
  );

  const actions = (
    <span className="flex flex-wrap items-center gap-2">
      {isAdmin && runnable.length > 0 && (
        <Button
          variant="secondary"
          className="gap-1.5"
          onClick={runAllNow}
          disabled={disabled || runAll.isPending || run.isPending}
          aria-busy={runAll.isPending || undefined}
          data-testid="organization-sources.run-all"
        >
          {runAll.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Running...")} /> : <Icon name="refresh-cw" size={16} aria-hidden />} {t_i18n("Run all syncs")}
        </Button>
      )}
      {(rows.length > 0 || variant === "section") && addSource}
    </span>
  );

  const summaryLine = rows.length > 0 && (
    <p className="mb-3 flex flex-wrap items-center gap-x-2 text-default-secondary" data-testid="organization-sources.summary">
      <span>{summary}</span>
      {failed.length > 0 && (
        <Button variant="link" className="h-auto p-0 text-feedback-error-primary" onClick={() => failed[0].sync && setHistory(failed[0].sync)}>
          {t_i18n("{count, plural, one {# failed} other {# failed}}", { count: failed.length })}
        </Button>
      )}
    </p>
  );
  const loaded = !syncsQuery.isLoading && !sourcesQuery.isLoading && !syncsQuery.isError && !sourcesQuery.isError;
  const table = (
    <DataTable
      rows={rows}
      columns={columns}
      layout="auto"
      rowKey={(row) => row.id}
      pinFirst={(row) => row.sync?.section === "all"}
      searchFields={(row) => [nameOf(row), t_i18n(SOURCE_KIND_NAMES[kindOf(row)]), row.sync ? filtersLine(row.sync) : ""]}
      searchPlaceholder={t_i18n("Search...")}
      filters={filters}
      exportName="organization-sources"
      urlKey={scope ? `osy_${scope}_` : "osy_"}
      storageKey={scope ? `organization-sources-${scope}` : "organization-sources"}
      isLoading={syncsQuery.isLoading || sourcesQuery.isLoading}
      isError={syncsQuery.isError || sourcesQuery.isError}
      onRetry={() => {
        void syncsQuery.refetch();
        void sourcesQuery.refetch();
      }}
      emptyLabel={t_i18n("No source feeds these sections yet")}
      rowTestId="organization-sources.row"
      rowActions={rowActions}
      rowActionsWidth={88}
    />
  );
  const dialogs = (
    <>
      <SyncEditDialog
        sync={editing}
        open={editing !== null}
        saving={update.isPending}
        onClose={() => setEditing(null)}
        onSave={(schedule, enabled, direction, filters) => {
          const done = { onSuccess: () => setEditing(null), onError: reportFailure(t_i18n("The sync could not be saved")) };
          if (editing) update.mutate({ id: editing.id, body: bodyOf(editing, { ...schedule, enabled, direction, ...(filters ? { filters } : {}) }) }, done);
        }}
      />
      <DeleteRowsDialog row={deleting} name={deleting ? nameOf(deleting) : ""} onClose={() => setDeleting(null)} />
      <SyncHistoryPanel sync={history} name={history ? nameOf({ id: "", sync: history, source: sourceOf(history.source) }) : ""} onClose={() => setHistory(null)} />
    </>
  );

  // A block of its host card: a sub-heading with its actions, the summary and the table; a short line when empty.
  if (variant === "section") {
    return (
      <>
        <section className="space-y-2" data-testid="organization-sources" aria-label={title}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0 space-y-0.5">
              {title && <h4 className="content-base font-semibold text-default-primary">{title}</h4>}
              {description && <p className="content-compact text-default-secondary">{description}</p>}
            </div>
            {actions}
          </div>
          {loaded && rows.length === 0 ? (
            <p className="text-default-secondary" data-testid="organization-sources.empty">
              {t_i18n("No source feeds these sections yet")}
            </p>
          ) : (
            <>
              {summaryLine}
              {table}
            </>
          )}
        </section>
        {dialogs}
      </>
    );
  }

  return (
    <>
      <SettingsCard
        data-testid="organization-sources"
        title={t_i18n("Sources and syncs")}
        description={t_i18n(
          "Who feeds these sections: the platforms each sync reads or writes on a schedule, the agents and partners writing through the API, CSV imports and rows entered by hand.",
        )}
        action={actions}
      >
        <OrganizationSharedContextNotice className="mb-4" />
        {summaryLine}
        {loaded && rows.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-md border border-dashed border-elevation-subtle px-6 py-10 text-center" data-testid="organization-sources.empty">
            <Icon name="refresh-cw" size={24} aria-hidden className="text-default-secondary" />
            <p className="title-sm">{t_i18n("No source feeds these sections yet")}</p>
            <p className="max-w-prose text-default-secondary">
              {t_i18n("Add a source: pick a platform, the section it fills and its filters, preview the rows, then keep them in sync on a schedule.")}
            </p>
            {addSource}
          </div>
        ) : (
          table
        )}
      </SettingsCard>
      {dialogs}
    </>
  );
}

const PlayIcon = () => <Icon name="play" size={16} aria-hidden />;
const PencilIcon = () => <Icon name="pencil" size={16} aria-hidden />;
const PauseIcon = () => <Icon name="circle-stop" size={16} aria-hidden />;
const HistoryIcon = () => <Icon name="history" size={16} aria-hidden />;
const TrashIcon = () => <Icon name="trash-2" size={16} aria-hidden />;
const PlusIcon = () => <Icon name="plus" size={16} aria-hidden />;

/** The rows a plan touches, per section, in one line: "1,186 assets, 2 asset groups". */
function usePlanLine() {
  const { t_i18n } = useFormatter();
  // Exact counts: a purge is confirmed on the number of rows it removes (#1276).
  return (plan: RowsPlan | undefined): string =>
    Object.entries(plan?.counts ?? {})
      .filter(([, count]) => count > 0)
      .map(([field, count]) =>
        isSourcedField(field)
          ? t_i18n(SECTION_COUNT_LABELS[field], { count })
          : t_i18n("{count, plural, one {# row} other {# rows}}", { count }),
      )
      .join(", ");
}

/**
 * Delete a sync, or clear a source with no sync, with the choice of what becomes of its rows:
 * keep them (a sync only), make them editable by hand, or purge them. The counts come from the
 * dry run of the API; rows another source also feeds are named and always kept.
 */
function DeleteRowsDialog({ row, name, onClose }: { row: Row | null; name: string; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const planLine = usePlanLine();
  const isSync = row?.sync != null;
  const [mode, setMode] = useState<SyncRowsMode>("leave");
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  if ((row?.id ?? null) !== loadedFor) {
    setLoadedFor(row?.id ?? null);
    setMode(row?.sync ? "leave" : "release");
  }
  const target = row ? (row.sync ? { sync: row.sync.id } : { source: row.source.key }) : null;
  const purge = useOrganizationRowsPreview(target, "purge");
  const release = useOrganizationRowsPreview(target, "release");
  const removeSync = useDeleteOrganizationSync();
  const applyRows = useApplyOrganizationSourceRows();
  const pending = removeSync.isPending || applyRows.isPending;
  const purgeLine = planLine(purge.data);
  const kept = Object.values(purge.data?.kept_elsewhere ?? {}).flat();

  const confirm = () => {
    if (!row) return;
    const fail = (error: unknown) =>
      toast({ title: t_i18n("The rows could not be changed"), description: error instanceof Error ? error.message : undefined, variant: "error" });
    if (row.sync) {
      removeSync.mutate(
        { id: row.sync.id, rows: mode },
        { onSuccess: () => (toast({ title: t_i18n("Sync deleted"), variant: "success" }), onClose()), onError: fail },
      );
    } else if (mode !== "leave") {
      applyRows.mutate(
        { source: row.source.key, mode },
        { onSuccess: (plan) => (toast({ title: t_i18n("{count, plural, one {# row changed} other {# rows changed}}", { count: plan.count }), variant: "success" }), onClose()), onError: fail },
      );
    }
  };

  return (
    <Dialog open={row !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="md" data-testid="organization-sources.delete">
        <DialogHeader>
          <DialogTitle>{isSync ? t_i18n("Delete the sync of {name}?", { name }) : t_i18n("Remove or hand over the rows of {name}?", { name })}</DialogTitle>
          <DialogDescription>
            {purge.isLoading
              ? t_i18n("Counting the rows it wrote...")
              : purgeLine
                ? t_i18n("It wrote {rows}.", { rows: purgeLine })
                : t_i18n("It wrote no row that is still in the profile.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <RadioGroup aria-label={t_i18n("What becomes of its rows")} value={mode} onValueChange={(next) => setMode(next as SyncRowsMode)} data-testid="organization-sources.delete-mode">
            {isSync && <Radio value="leave" label={t_i18n("Keep the rows as they are")} description={t_i18n("They stay marked as coming from {name}; a new sync of it takes them back.", { name })} />}
            <Radio
              value="release"
              label={t_i18n("Keep the rows, editable by hand")}
              description={t_i18n("{count, plural, =0 {No row changes.} one {# row becomes yours to edit.} other {# rows become yours to edit.}}", { count: release.data?.count ?? 0 })}
            />
            <Radio
              value="purge"
              label={t_i18n("Purge the rows it wrote")}
              description={t_i18n("{count, plural, =0 {No row is removed.} one {# row is removed.} other {# rows are removed.}} Rows a person wrote or another source feeds are kept.", {
                count: purge.data?.count ?? 0,
              })}
            />
          </RadioGroup>
          {mode === "purge" && kept.length > 0 && (
            <Alert
              severity="info"
              elevation={1}
              data-testid="organization-sources.delete-kept"
              title={t_i18n("{count, plural, one {# row with the same name from another source is kept} other {# rows with the same name from other sources are kept}}: {names}", {
                count: kept.length,
                names: kept.slice(0, 5).join(", ") + (kept.length > 5 ? ", ..." : ""),
              })}
            />
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button variant={mode === "purge" ? "destructive" : "default"} onClick={confirm} disabled={pending || (!isSync && mode === "leave")} aria-busy={pending || undefined}>
            {pending && <Spinner size="sm" tone="inherit" label={t_i18n("Saving...")} />}
            {isSync
              ? mode === "purge"
                ? t_i18n("Delete and purge {count, plural, one {# row} other {# rows}}", { count: purge.data?.count ?? 0 })
                : t_i18n("Delete the sync")
              : mode === "purge"
                ? t_i18n("Purge {count, plural, one {# row} other {# rows}}", { count: purge.data?.count ?? 0 })
                : t_i18n("Make the rows editable")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The last runs of a sync and its saves, newest first, with their counts or their error. */
function SyncHistoryPanel({ sync, name, onClose }: { sync: OrganizationSync | null; name: string; onClose: () => void }) {
  const { t_i18n, locale } = useFormatter();
  const { data, isLoading, isError, refetch } = useOrganizationSyncHistory(sync?.id ?? null);
  const entries = Array.isArray(data) ? data : [];
  const when = (iso: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  return (
    <SlideOver open={sync !== null} onClose={onClose} label={t_i18n("History of {name}", { name })} width="w-[480px]">
      <div className="flex items-center justify-between gap-3 border-b border-elevation-subtle px-5 py-4">
        <h2 className="title-sm">{t_i18n("History of {name}", { name })}</h2>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Close")} onClick={onClose}>
          <Icon name="x" size={16} aria-hidden />
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-4" data-testid="organization-sources.history">
        {isLoading ? (
          <Spinner label={t_i18n("Loading...")} />
        ) : isError ? (
          <Alert severity="error" title={t_i18n("The history could not be read")} action={<Button variant="secondary" onClick={() => void refetch()}>{t_i18n("Try again")}</Button>} />
        ) : entries.length === 0 ? (
          <p className="text-default-secondary">{t_i18n("No run changed the profile yet.")}</p>
        ) : (
          <ol className="space-y-3">
            {entries.map((entry) => (
              <li key={`${entry.at}-${entry.action}`} className="rounded-md border border-elevation-subtle p-3" data-testid="organization-sources.history-entry">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{when(entry.at)}</span>
                  {entry.error ? <Badge variant="destructive">{t_i18n("Failed")}</Badge> : <Badge variant="outline">{t_i18n(HISTORY_KIND_LABELS[historyKindOf(entry)])}</Badge>}
                </div>
                {/* Built from the entry's fields, never the backend's English summary (UX r1 B7). */}
                {entry.error ? (
                  <p className="text-feedback-error-primary">{entry.error}</p>
                ) : entry.added !== null || entry.updated !== null || entry.removed !== null ? (
                  <p className="text-default-secondary">
                    {t_i18n("{added} added, {updated} updated, {removed} removed", { added: entry.added ?? 0, updated: entry.updated ?? 0, removed: entry.removed ?? 0 })}
                  </p>
                ) : null}
                {entry.by && <p className="content-compact text-default-secondary">{t_i18n("By {who}", { who: entry.by })}</p>}
              </li>
            ))}
          </ol>
        )}
      </div>
    </SlideOver>
  );
}
