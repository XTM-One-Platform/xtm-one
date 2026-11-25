// SPDX-License-Identifier: Apache-2.0

/**
 * The edit dialog of one organization sync (#11913, #1276): its direction (#1134 S3: Inbound,
 * the platform is the master, or Outbound, the XTM One list is, offered only where the platform
 * can be written), its filters (#1982 S4: the same fields as the add stepper, a change previewed
 * before it is saved), its schedule and its switch; a Sync all saved before #1548 its schedule alone (none is created). Saving a
 * paused sync resumes it with the token of the administrator who saves it. Mounted by the one
 * Sources and syncs table (OrganizationSourcesSyncs), with the labels the table shares.
 */
import { useState } from "react";
import { Alert, Radio, RadioGroup, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { CompactFigure } from "@/components/ui/compact-figure";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SettingsField, SettingsGroupDescription, SettingsGroupTitle, SettingsSwitchField, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useOrganizationSyncDirections,
  usePreviewOrganizationSyncChange,
  type ImportSectionKey,
  type ImportSourceKey,
  type OrganizationSync,
  type OrganizationSyncBody,
  type SyncChangePreview,
  type SyncDirection,
} from "@/hooks/api/useOrganizationContextApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { IMPORT_SOURCE_NAMES } from "@/lib/organization-profile";
import { defaultSchedule, type SyncSchedule } from "@/lib/organization-sync-schedule";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";
import { SyncFilterFields, type SyncFilters } from "./OrganizationSyncFilters";
import { reasonLabel, SyncPreviewError, SyncRowList } from "./OrganizationSyncPreviewParts";
import { SyncScheduleFields } from "./OrganizationSyncSchedule";

/** @i18n-keys */
export const SYNC_SECTION_LABELS = {
  all: "Sync all",
  assets: "Assets",
  asset_groups: "Asset groups",
  software: "Software",
  primary_assets: "Primary assets",
  pirs: "PIRs",
} as const satisfies Record<ImportSectionKey | "all", string>;

export type SyncState = "enabled" | "paused" | "disabled";

/** @i18n-keys */
export const DIRECTION_LABELS = {
  inbound: "Inbound",
  outbound: "Outbound",
} as const satisfies Record<SyncDirection, string>;

export const directionOf = (sync: OrganizationSync): SyncDirection => sync.direction ?? "inbound";

export const stateOf = (sync: OrganizationSync): SyncState => (!sync.enabled ? "disabled" : sync.paused_reason ? "paused" : "enabled");

export const bodyOf = (sync: OrganizationSync, patch: Partial<OrganizationSyncBody> = {}): OrganizationSyncBody => ({
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

interface EditProps {
  /** The sync edited; null while the dialog is closed (no Sync all is created from the UI, #1548). */
  sync: OrganizationSync | null;
  open: boolean;
  saving: boolean;
  onClose: () => void;
  /** The direction is sent only for a section sync; Sync all has none. The filters only when this dialog changed them. */
  onSave: (schedule: SyncSchedule, enabled: boolean, direction: SyncDirection | undefined, filters?: OrganizationSync["filters"]) => void;
}

/** The words of each count of the change preview, by direction (#1982 S4). @i18n-keys */
const CHANGE_LABELS = {
  inbound: { enter: "To add", update: "To update", leave: "To remove", kept: "Kept" },
  outbound: { enter: "To create", update: "To update", leave: "To remove" },
} as const satisfies Record<SyncDirection, Partial<Record<keyof SyncChangePreview["counts"], string>>>;
type ChangeKey = keyof SyncChangePreview["counts"];

/** A filter set without its empty values, so an emptied picker compares equal to an unset one. */
const cleanFilters = (filters: SyncFilters | null | undefined): SyncFilters =>
  Object.fromEntries(Object.entries(filters ?? {}).filter(([, value]) => (Array.isArray(value) ? value.length > 0 : Boolean(value))));

const sameFilters = (a: SyncFilters | null | undefined, b: SyncFilters | null | undefined): boolean => {
  const left = cleanFilters(a);
  const right = cleanFilters(b);
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].every((key) => {
    const one = left[key];
    const two = right[key];
    const list = (value: string | string[] | undefined) => (Array.isArray(value) ? [...value].sort() : value ? [value] : []);
    return JSON.stringify(list(one)) === JSON.stringify(list(two));
  });
};

/**
 * The direction, filters, schedule and switch of one sync. New filters are saved only after a dry run of the next run
 * (POST /syncs/{id}/preview): the rows that enter, are updated, leave and are removed, before anything is written.
 * Saving again resumes a paused sync with the current administrator's token.
 */
export function SyncEditDialog({ sync, open, saving, onClose, onSave }: EditProps) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat();
  const { data: me } = useMe(open);
  const meLabel = me?.email || me?.full_name || null;
  const [schedule, setSchedule] = useState<SyncSchedule>(defaultSchedule);
  const [enabled, setEnabled] = useState(true);
  const [direction, setDirection] = useState<SyncDirection>("inbound");
  const [filters, setFilters] = useState<SyncFilters>({});
  const [previewed, setPreviewed] = useState(false);
  const change = usePreviewOrganizationSyncChange();
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const sectionSync = sync !== null && sync.section !== "all";
  const { data: matrix } = useOrganizationSyncDirections(open && sectionSync);
  const support = sectionSync ? (Array.isArray(matrix) ? matrix : []).find((row) => row.section === sync.section && row.source === sync.source) : undefined;
  // Outbound is offered only when the platform can be written; a stored outbound stays pickable so it is never hidden.
  const outboundOffered = (Array.isArray(support?.directions) && support.directions.includes("outbound")) || (sync ? directionOf(sync) === "outbound" : false);
  const platform = sync ? (IMPORT_SOURCE_NAMES[sync.source] ?? sync.source) : "";
  const directionChanged = sectionSync && direction !== directionOf(sync);
  const filtersChanged = sectionSync && !sameFilters(filters, sync.filters);
  // A new direction starts clean (no preview: nothing it created yet); new filters on the same direction are previewed first.
  const needsPreview = filtersChanged && !directionChanged;
  const key = open ? (sync?.id ?? "new") : null;
  if (key !== loadedFor) {
    setLoadedFor(key);
    if (key !== null) {
      setSchedule(sync ? { frequency: sync.frequency, hour: sync.hour, minute: sync.minute, weekday: sync.weekday, timezone: sync.timezone } : defaultSchedule());
      setEnabled(sync ? sync.enabled : true);
      setDirection(sync ? directionOf(sync) : "inbound");
      setFilters(cleanFilters(sync?.filters));
      setPreviewed(false);
      change.reset();
    }
  }
  const changeFilters = (next: SyncFilters) => {
    // A preview belongs to the filters it read: a new edit asks for a new one.
    setFilters(next);
    setPreviewed(false);
    change.reset();
  };
  const pickDirection = (next: SyncDirection) => {
    setDirection(next);
    // The filters of one direction do not apply to the other: back to the stored ones, or none for a new direction.
    changeFilters(sync && next === directionOf(sync) ? cleanFilters(sync.filters) : {});
  };
  const previewChange = () => {
    if (!sync) return;
    change.mutate({ id: sync.id, body: { filters: cleanFilters(filters), batch_type: sync.batch_type, remove_gone: sync.remove_gone } }, { onSuccess: () => setPreviewed(true) });
  };
  const title = t_i18n("Edit the sync");
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {sync?.paused_reason
              ? t_i18n("Paused: {reason} Saving resumes it with your own platform token.", { reason: sync.paused_reason })
              : t_i18n("Saving it makes your own platform token the one its runs read with.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <OrganizationSharedContextNotice
            elevation={1}
            direction={sectionSync ? direction : "inbound"}
            actor={sync ? { mode: "runs", who: sync.owner_email ?? t_i18n("Unknown"), savingAs: meLabel } : null}
          />
          {sectionSync && (
            <SettingsField
              id="org-sync-edit-direction"
              label={t_i18n("Direction")}
              info={t_i18n(
                "Which side is the master of this list. Inbound: the platform's rows are copied here, read-only. Outbound: the XTM One list is written to the platform, for example primary assets sent to OpenCRQ as supporting assets. A sync saved without a direction is Inbound.",
              )}
            >
              <RadioGroup
                id="org-sync-edit-direction"
                aria-labelledby={settingsFieldLabelId("org-sync-edit-direction")}
                data-testid="org-sync-edit-direction"
                value={direction}
                onValueChange={(next) => pickDirection(next === "outbound" ? "outbound" : "inbound")}
              >
                <Radio
                  value="inbound"
                  label={t_i18n(DIRECTION_LABELS.inbound)}
                  description={t_i18n("{platform} is the master: its rows are read-only here and follow {platform}.", { platform })}
                />
                <Radio
                  value="outbound"
                  disabled={!outboundOffered}
                  label={t_i18n(DIRECTION_LABELS.outbound)}
                  description={
                    !outboundOffered && support?.reason ? (
                      <span data-testid="org-sync-edit-direction.reason">
                        {t_i18n("Outbound is not offered: {reason}", { reason: reasonLabel(t_i18n, support.reason) })}
                      </span>
                    ) : (
                      t_i18n(
                        "The XTM One list is the master: the {platform} rows this sync creates are updated and deleted to match it. Other {platform} rows are never touched.",
                        { platform },
                      )
                    )
                  }
                />
              </RadioGroup>
            </SettingsField>
          )}
          {directionChanged && (
            <Alert
              severity="warning"
              elevation={1}
              data-testid="org-sync-edit-direction.changed"
              title={t_i18n(
                "A new direction starts clean: the rows the previous direction created stay in {platform}, untouched, and are not taken over. The next run follows the new direction.",
                { platform },
              )}
            />
          )}
          <SyncScheduleFields idPrefix="org-sync-edit" value={schedule} onChange={setSchedule} direction={sectionSync ? direction : "inbound"} />
          {sectionSync && (
            <div role="group" aria-labelledby="org-sync-edit-filters-title" className="space-y-4" data-testid="org-sync-edit-filters">
              <div>
                <SettingsGroupTitle id="org-sync-edit-filters-title">{t_i18n("Filters")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {directionChanged
                    ? t_i18n("A new direction starts with no filter: pick the ones its runs use.")
                    : direction === "outbound"
                      ? t_i18n("Which XTM One rows the runs push to {platform}.", { platform })
                      : t_i18n("Which rows the runs import from {platform}.", { platform })}
                </SettingsGroupDescription>
              </div>
              <SyncFilterFields section={sync.section as ImportSectionKey} source={sync.source as ImportSourceKey} direction={direction} filters={filters} onChange={changeFilters} active={open} />
            </div>
          )}
          <SettingsSwitchField
            id="org-sync-edit-enabled"
            label={t_i18n("Enabled")}
            helper={t_i18n("Off: the sync keeps its settings and does not run until it is switched on.")}
            checked={enabled}
            onCheckedChange={setEnabled}
          />
          {needsPreview && (change.isPending || change.isError || change.data) && (
            <div className="space-y-3" data-testid="org-sync-edit-change" aria-live="polite">
              {change.isPending && (
                <p className="flex items-center gap-2 content-compact text-default-secondary">
                  <Spinner size="sm" />
                  {t_i18n("Reading {platform}", { platform })}
                </p>
              )}
              <SyncPreviewError
                platform={platform}
                requestError={change.isError ? change.error : null}
                readFailed={Boolean(change.data?.error)}
                cause={change.data?.error}
                onRetry={previewChange}
                retrying={change.isPending}
                testId="org-sync-edit-change.error"
              />
              {change.data && !change.data.error && (
                <>
                  <p className="content-compact text-default-secondary">{t_i18n("What the next run does with these filters. Nothing is saved before Save the change.")}</p>
                  <dl className={direction === "outbound" ? "grid grid-cols-3 gap-2" : "grid grid-cols-4 gap-2"} data-testid="org-sync-edit-change.counts">
                    {(Object.entries(CHANGE_LABELS[direction]) as [ChangeKey, string][]).map(([key, label]) => (
                      <div key={key} className="rounded-sm border border-elevation-subtle px-3 py-2">
                        <dt className="content-compact text-default-secondary">{t_i18n(label)}</dt>
                        <dd className="content-base font-semibold text-default-primary">
                          <CompactFigure exact={f.exactCount(change.data.counts[key])}>{f.count(change.data.counts[key])}</CompactFigure>
                        </dd>
                      </div>
                    ))}
                  </dl>
                  {change.data.leave.length > 0 && (
                    <div className="space-y-1" data-testid="org-sync-edit-change.leave">
                      <p className="content-compact font-medium text-default-primary">
                        {direction === "outbound"
                          ? t_i18n("Deleted from {platform} on the next run: only rows this sync created.", { platform })
                          : t_i18n("Removed from the list on the next run: rows this sync wrote that leave the filters.")}
                      </p>
                      <SyncRowList rows={change.data.leave} total={Math.max(change.data.counts.leave, change.data.leave.length)} testId="org-sync-edit-change.leave-rows" />
                    </div>
                  )}
                  {change.data.kept.length > 0 && (
                    <p className="content-compact text-default-secondary" data-testid="org-sync-edit-change.kept">
                      {t_i18n("{count, plural, one {# row leaves the filters but stays: another source wrote it too.} other {# rows leave the filters but stay: another source wrote them too.}}", {
                        count: change.data.counts.kept,
                      })}
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          {needsPreview && !previewed ? (
            <Button onClick={previewChange} disabled={change.isPending} aria-busy={change.isPending || undefined}>
              {change.isPending && <Spinner size="sm" tone="inherit" label={t_i18n("Loading")} />}
              {t_i18n("Preview the change")}
            </Button>
          ) : (
            <Button
              onClick={() => onSave(schedule, enabled, sectionSync ? direction : undefined, filtersChanged ? cleanFilters(filters) : undefined)}
              disabled={saving || (needsPreview && Boolean(change.data?.error))}
              aria-busy={saving || undefined}
            >
              {saving && <Spinner size="sm" tone="inherit" label={t_i18n("Saving...")} />}
              {needsPreview ? t_i18n("Save the change") : t_i18n("Save")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
