// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Syncs (#11913): the imports saved with a schedule.
 *
 * One row per sync: the section, the source with its logo, the filters, the
 * schedule, the last run (when, added, updated, removed, or its error), the
 * next run, the status (Enabled, Paused with its reason, Disabled) and the
 * enabled switch; Run now, Edit and Delete on each row. "Sync all" is one
 * more schedule that runs every enabled section sync in order. A sync is
 * created from the import stepper ("Keep in sync"); a paused sync resumes when
 * it is saved again, with the token of the administrator who saves it.
 */
import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SettingsCard, SettingsSwitchField } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useCreateOrganizationSync,
  useDeleteOrganizationSync,
  useOrganizationSyncs,
  useRunOrganizationSync,
  useUpdateOrganizationSync,
  type ImportSectionKey,
  type OrganizationSync,
  type OrganizationSyncBody,
  type OrganizationSyncScope,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { filtersSummary } from "@/lib/organization-import-filters";
import { PLATFORM_NAMES } from "@/lib/organization-profile";
import { formatTimeAgo } from "@/lib/relative-time";
import { timeUntil } from "@/lib/utils";
import { defaultSchedule, useScheduleLabel, type SyncSchedule } from "@/lib/organization-sync-schedule";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";
import { SyncScheduleFields } from "./OrganizationSyncSchedule";

/** @i18n-keys */
const SYNC_SECTION_LABELS = {
  all: "Sync all",
  assets: "Assets",
  asset_groups: "Asset groups",
  ai_systems: "AI systems",
  tech_stack: "Tech stack",
  crown_jewels: "Crown jewels",
  pirs: "PIRs",
} as const satisfies Record<ImportSectionKey | "all", string>;

type SyncState = "enabled" | "paused" | "disabled";

/** @i18n-keys */
const STATE_LABELS = {
  enabled: "Enabled",
  paused: "Paused",
  disabled: "Disabled",
} as const satisfies Record<SyncState, string>;

const stateOf = (sync: OrganizationSync): SyncState => (!sync.enabled ? "disabled" : sync.paused_reason ? "paused" : "enabled");

const bodyOf = (sync: OrganizationSync, patch: Partial<OrganizationSyncBody> = {}): OrganizationSyncBody => ({
  section: sync.section,
  source: sync.source,
  filters: sync.filters,
  batch_type: sync.batch_type,
  remove_gone: sync.remove_gone,
  frequency: sync.frequency,
  hour: sync.hour,
  minute: sync.minute,
  weekday: sync.weekday,
  timezone: sync.timezone,
  enabled: sync.enabled,
  ...patch,
});

interface CardProps {
  /**
   * The sections this card lists (#1134 S2): "profile" (crown jewels, PIRs) in Settings,
   * "inventory" (assets, asset groups, tech stack, AI systems) on the Inventory page, "" every sync.
   * The Sync all schedule shows in each; "" is kept for a caller that lists them all.
   */
  scope?: OrganizationSyncScope;
}

export function OrganizationSyncsCard({ scope = "" }: CardProps = {}) {
  const { t_i18n, locale, intl } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const scheduleLabel = useScheduleLabel();
  const { data: syncs = [], isLoading, isError, refetch } = useOrganizationSyncs(scope);
  const create = useCreateOrganizationSync();
  const update = useUpdateOrganizationSync();
  const remove = useDeleteOrganizationSync();
  const run = useRunOrganizationSync();
  const [editing, setEditing] = useState<OrganizationSync | "all" | null>(null);
  const hasSyncAll = syncs.some((sync) => sync.section === "all");

  const reportFailure = (title: string) => (error: unknown) =>
    toast({ title, description: error instanceof Error ? error.message : undefined, variant: "error" });

  const sourceName = (sync: OrganizationSync) => (sync.section === "all" ? t_i18n("Every section sync") : (PLATFORM_NAMES[sync.source] ?? sync.source));

  /** The filters of a sync in the import stepper's words: "Category: Host, Server; Labels: 2 values", or every entity. */
  const filtersLine = (sync: OrganizationSync): string => {
    if (sync.section === "all") return t_i18n("In the order they were saved");
    return filtersSummary(t_i18n, locale, sync.filters) ?? t_i18n("No filter: every entity");
  };

  /** The last run in one line: when, then added, updated and removed, or its error. */
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

  const toggleEnabled = (sync: OrganizationSync, enabled: boolean) =>
    update.mutate({ id: sync.id, body: bodyOf(sync, { enabled }) }, { onError: reportFailure(t_i18n("The sync could not be saved")) });

  const runNow = (sync: OrganizationSync) =>
    run.mutate(sync.id, {
      onSuccess: (done) =>
        done.last_result?.error
          ? toast({ title: t_i18n("The sync ran and failed"), description: done.paused_reason || undefined, variant: "error" })
          : toast({ title: t_i18n("Sync run"), variant: "success" }),
      onError: reportFailure(t_i18n("The sync could not run")),
    });

  const deleteSync = async (sync: OrganizationSync) => {
    const confirmed = await confirm({
      title: "Delete this sync?",
      description: "Its schedule stops. The rows it already imported stay in the profile.",
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!confirmed) return;
    remove.mutate(sync.id, {
      onSuccess: () => toast({ title: t_i18n("Sync deleted"), variant: "success" }),
      onError: reportFailure(t_i18n("The sync could not be deleted")),
    });
  };

  const columns: DataTableColumn<OrganizationSync>[] = [
    {
      key: "section",
      header: "Section",
      cell: (sync) => <span className="whitespace-nowrap font-medium">{t_i18n(SYNC_SECTION_LABELS[sync.section])}</span>,
      sortValue: (sync) => (sync.section === "all" ? "" : t_i18n(SYNC_SECTION_LABELS[sync.section])),
      firstSort: "asc",
      csv: (sync) => sync.section,
    },
    {
      key: "source",
      header: "Source",
      cell: (sync) => (
        <span className="flex min-w-0 items-center gap-2">
          {sync.source && <ProviderLogo provider={sync.source} decorative className="h-4 w-4 shrink-0" />}
          <TruncatedText text={sourceName(sync)} className="max-w-[10rem]" />
        </span>
      ),
      sortValue: (sync) => sourceName(sync),
      csv: (sync) => sync.source,
    },
    {
      // #11994: whose permissions the runs read the platform with.
      key: "runs_as",
      header: "Runs as",
      hideable: true,
      hideBelow: "4xl",
      cell: (sync) => <TruncatedText text={sync.owner_email ?? t_i18n("Unknown")} className="max-w-[12rem]" />,
      sortValue: (sync) => sync.owner_email ?? "",
      csv: (sync) => sync.owner_email ?? "",
    },
    {
      key: "filters",
      header: "Filters",
      hideable: true,
      hideBelow: "6xl",
      cell: (sync) => <TruncatedText text={filtersLine(sync)} lines={2} className="w-max max-w-[14rem] content-compact text-default-secondary" />,
      csv: (sync) => filtersLine(sync),
    },
    {
      key: "schedule",
      header: "Schedule",
      hideable: true,
      cell: (sync) => <TruncatedText text={scheduleLabel(sync)} lines={2} className="w-max max-w-[13rem]" />,
      sortValue: (sync) => sync.frequency,
      csv: (sync) => scheduleLabel(sync),
    },
    {
      key: "last_run",
      header: "Last run",
      hideable: true,
      hideBelow: "4xl",
      cell: (sync) =>
        sync.last_result?.error ? (
          <Tip content={sync.last_result.error}>
            <span className="block w-max max-w-[14rem] line-clamp-2 text-feedback-error-primary" tabIndex={0}>
              {lastRunLine(sync)}
            </span>
          </Tip>
        ) : (
          <TruncatedText text={lastRunLine(sync)} lines={2} className="w-max max-w-[14rem] text-default-secondary" />
        ),
      sortValue: (sync) => sync.last_run_at ?? "",
      csv: (sync) => sync.last_run_at ?? "",
    },
    {
      key: "next_run",
      header: "Next run",
      hideable: true,
      hideBelow: "6xl",
      cell: (sync) => (
        <span className="whitespace-nowrap text-default-secondary">{sync.enabled && !sync.paused_reason && sync.next_run_at ? timeUntil(sync.next_run_at, locale) : t_i18n("Not scheduled")}</span>
      ),
      sortValue: (sync) => sync.next_run_at ?? "",
      csv: (sync) => sync.next_run_at ?? "",
    },
    {
      key: "status",
      header: "Status",
      cell: (sync) => {
        const state = stateOf(sync);
        const badge = (
          <Badge variant={state === "paused" ? "warning" : "outline"} className="whitespace-nowrap">
            {t_i18n(STATE_LABELS[state])}
          </Badge>
        );
        return state === "paused" ? (
          <Tip content={t_i18n("Paused: {reason}", { reason: sync.paused_reason })}>
            <span tabIndex={0}>{badge}</span>
          </Tip>
        ) : (
          badge
        );
      },
      sortValue: (sync) => stateOf(sync),
      csv: (sync) => stateOf(sync),
    },
    {
      key: "enabled",
      header: "Enabled",
      interactive: true,
      cell: (sync) => (
        <Switch
          checked={sync.enabled}
          onCheckedChange={(enabled) => toggleEnabled(sync, enabled)}
          disabled={update.isPending}
          aria-label={t_i18n("Enabled")}
        />
      ),
      sortValue: (sync) => sync.enabled,
      csv: (sync) => sync.enabled,
    },
  ];

  const filters: DataTableFilter<OrganizationSync>[] = [
    {
      key: "status",
      label: "Status",
      values: (sync) => [stateOf(sync)],
      options: (Object.keys(STATE_LABELS) as SyncState[]).map((state) => ({ value: state, label: t_i18n(STATE_LABELS[state]) })),
    },
    {
      key: "source",
      label: "Source",
      values: (sync) => [sync.source || "all"],
      options: [
        ...Object.entries(PLATFORM_NAMES).map(([value, label]) => ({ value, label })),
        { value: "all", label: t_i18n("Every section sync") },
      ],
      pinned: ["all"],
    },
  ];

  const rowActions = (sync: OrganizationSync) => (
    <span className="inline-flex gap-1">
      <Tip content={t_i18n("Run now")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Run now")} onClick={() => runNow(sync)} disabled={run.isPending}>
          {run.isPending && run.variables === sync.id ? <Spinner size="sm" tone="inherit" label={t_i18n("Running...")} /> : <Icon name="play" size={16} aria-hidden />}
        </Button>
      </Tip>
      <Tip content={t_i18n("Edit")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Edit")} onClick={() => setEditing(sync)}>
          <Icon name="pencil" size={16} aria-hidden />
        </Button>
      </Tip>
      <Tip content={t_i18n("Delete")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Delete")} onClick={() => void deleteSync(sync)} disabled={remove.isPending}>
          <Icon name="trash-2" size={16} aria-hidden />
        </Button>
      </Tip>
    </span>
  );

  return (
    <>
      {ConfirmEl}
      <SettingsCard
        data-testid="settings.organization.syncs"
        title={t_i18n("Syncs")}
        description={t_i18n("Imports from the platforms that run again on a schedule. Add one with Keep in sync in Draft > From the platforms.")}
        action={
          !hasSyncAll &&
          syncs.length > 0 && (
            <Button variant="secondary" className="gap-1.5" onClick={() => setEditing("all")}>
              <Icon name="plus" size={16} aria-hidden /> {t_i18n("Add Sync all")}
            </Button>
          )
        }
      >
        <OrganizationSharedContextNotice className="mb-4" />
        <DataTable
          rows={syncs}
          columns={columns}
          layout="auto"
          rowKey={(sync) => sync.id}
          pinFirst={(sync) => sync.section === "all"}
          searchFields={(sync) => [t_i18n(SYNC_SECTION_LABELS[sync.section]), sourceName(sync), filtersLine(sync)]}
          searchPlaceholder={t_i18n("Search...")}
          filters={filters}
          exportName="organization-syncs"
          urlKey={scope ? `osy_${scope}_` : "osy_"}
          storageKey={scope ? `settings-organization-syncs-${scope}` : "settings-organization-syncs"}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No sync yet: imports run once. Turn on Keep in sync on an import's preview to schedule it.")}
          rowTestId="organization-sync-row"
          rowActions={rowActions}
          rowActionsWidth={120}
        />
      </SettingsCard>

      <SyncEditDialog
        sync={editing === "all" || editing === null ? null : editing}
        open={editing !== null}
        saving={create.isPending || update.isPending}
        onClose={() => setEditing(null)}
        onSave={(schedule, enabled) => {
          const done = { onSuccess: () => setEditing(null), onError: reportFailure(t_i18n("The sync could not be saved")) };
          if (editing === "all") create.mutate({ section: "all", source: "", filters: {}, batch_type: "", remove_gone: false, ...schedule, enabled }, done);
          else if (editing) update.mutate({ id: editing.id, body: bodyOf(editing, { ...schedule, enabled }) }, done);
        }}
      />
    </>
  );
}

interface EditProps {
  /** null: a new Sync all. */
  sync: OrganizationSync | null;
  open: boolean;
  saving: boolean;
  onClose: () => void;
  onSave: (schedule: SyncSchedule, enabled: boolean) => void;
}

/** The schedule and the switch of one sync; saving it again resumes a paused one with the current administrator's token. */
function SyncEditDialog({ sync, open, saving, onClose, onSave }: EditProps) {
  const { t_i18n } = useFormatter();
  const { data: me } = useMe(open);
  const meLabel = me?.email || me?.full_name || null;
  const [schedule, setSchedule] = useState<SyncSchedule>(defaultSchedule);
  const [enabled, setEnabled] = useState(true);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const key = open ? (sync?.id ?? "new") : null;
  if (key !== loadedFor) {
    setLoadedFor(key);
    if (key !== null) {
      setSchedule(sync ? { frequency: sync.frequency, hour: sync.hour, minute: sync.minute, weekday: sync.weekday, timezone: sync.timezone } : defaultSchedule());
      setEnabled(sync ? sync.enabled : true);
    }
  }
  const title = sync ? t_i18n("Edit the sync") : t_i18n("Add Sync all");
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {sync?.paused_reason
              ? t_i18n("Paused: {reason} Saving resumes it with your own platform token.", { reason: sync.paused_reason })
              : sync
                ? t_i18n("Saving it makes your own platform token the one its runs read with.")
                : t_i18n("Runs every enabled section sync, in the order they were saved, on this schedule.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <OrganizationSharedContextNotice elevation={1} actor={sync ? { mode: "runs", who: sync.owner_email ?? t_i18n("Unknown"), savingAs: meLabel } : null} />
          <SyncScheduleFields idPrefix="org-sync-edit" value={schedule} onChange={setSchedule} />
          <SettingsSwitchField
            id="org-sync-edit-enabled"
            label={t_i18n("Enabled")}
            helper={t_i18n("Off: the sync keeps its settings and does not run until it is switched on.")}
            checked={enabled}
            onCheckedChange={setEnabled}
          />
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={() => onSave(schedule, enabled)} disabled={saving} aria-busy={saving || undefined}>
            {saving && <Spinner size="sm" tone="inherit" label={t_i18n("Saving...")} />}
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
