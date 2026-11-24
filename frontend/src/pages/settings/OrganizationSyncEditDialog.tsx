// SPDX-License-Identifier: Apache-2.0

/**
 * The edit dialog of one organization sync (#11913, #1276): its direction (#1134 S3: Inbound,
 * the platform is the master, or Outbound, the XTM One list is, offered only where the platform
 * can be written), its schedule and its switch; a Sync all saved before #1548 its schedule alone (none is created). Saving a
 * paused sync resumes it with the token of the administrator who saves it. Mounted by the one
 * Sources and syncs table (OrganizationSourcesSyncs), with the labels the table shares.
 */
import { useState } from "react";
import { Alert, Radio, RadioGroup, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SettingsField, SettingsSwitchField, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useOrganizationSyncDirections,
  type ImportSectionKey,
  type OrganizationSync,
  type OrganizationSyncBody,
  type SyncDirection,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { linkOptionOn } from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES } from "@/lib/organization-profile";
import { defaultSchedule, type SyncSchedule } from "@/lib/organization-sync-schedule";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";
import { SyncScheduleFields } from "./OrganizationSyncSchedule";

/** @i18n-keys */
export const SYNC_SECTION_LABELS = {
  all: "Sync all",
  assets: "Assets",
  asset_groups: "Asset groups",
  ai_systems: "AI systems",
  software: "Software",
  crown_jewels: "Primary assets",
  pirs: "PIRs",
} as const satisfies Record<ImportSectionKey | "all", string>;

export type SyncState = "enabled" | "paused" | "disabled";

/** @i18n-keys */
export const DIRECTION_LABELS = {
  inbound: "Inbound",
  outbound: "Outbound",
} as const satisfies Record<SyncDirection, string>;

/**
 * The reasons GET /syncs/directions gives for a pairing without outbound (backend DIRECTIONS of
 * organization_context_outbound.py), listed so the catalogs translate them; an unknown one shows as sent.
 * @i18n-keys
 */
const OUTBOUND_REASONS = {
  openaev_asset_groups: "Planned: OpenAEV asset group writes are not wired for syncs yet.",
  opencti_pirs: "Planned: OpenCTI PIR writes are not wired for syncs yet.",
  opencti_assets: "OpenCTI Systems are owned by its connectors.",
  openaev_software: "OpenAEV software inventory is read from its agents.",
  other: "This pairing reads only.",
} as const;

const KNOWN_REASONS: readonly string[] = Object.values(OUTBOUND_REASONS);

const reasonLabel = (t_i18n: (key: string) => string, reason: string): string => (KNOWN_REASONS.includes(reason) ? t_i18n(reason) : reason);

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

/** Whether a sync carries the OpenCRQ link option of #1591 (the primary-asset source of CTEM > Inventory > Sources, #1754). */
const hasLinkOption = (sync: OrganizationSync | null): sync is OrganizationSync => sync !== null && sync.section === "crown_jewels" && sync.source === "opencrq";

/** The schedule and the switch of one sync; saving it again resumes a paused one with the current administrator's token. */
export function SyncEditDialog({ sync, open, saving, onClose, onSave }: EditProps) {
  const { t_i18n } = useFormatter();
  const { data: me } = useMe(open);
  const meLabel = me?.email || me?.full_name || null;
  const [schedule, setSchedule] = useState<SyncSchedule>(defaultSchedule);
  const [enabled, setEnabled] = useState(true);
  const [direction, setDirection] = useState<SyncDirection>("inbound");
  const [linkOn, setLinkOn] = useState(true);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const sectionSync = sync !== null && sync.section !== "all";
  const { data: matrix } = useOrganizationSyncDirections(open && sectionSync);
  const support = sectionSync ? (Array.isArray(matrix) ? matrix : []).find((row) => row.section === sync.section && row.source === sync.source) : undefined;
  // Outbound is offered only when the platform can be written; a stored outbound stays pickable so it is never hidden.
  const outboundOffered = (Array.isArray(support?.directions) && support.directions.includes("outbound")) || (sync ? directionOf(sync) === "outbound" : false);
  const platform = sync ? (IMPORT_SOURCE_NAMES[sync.source] ?? sync.source) : "";
  const directionChanged = sectionSync && direction !== directionOf(sync);
  const key = open ? (sync?.id ?? "new") : null;
  if (key !== loadedFor) {
    setLoadedFor(key);
    if (key !== null) {
      setSchedule(sync ? { frequency: sync.frequency, hour: sync.hour, minute: sync.minute, weekday: sync.weekday, timezone: sync.timezone } : defaultSchedule());
      setEnabled(sync ? sync.enabled : true);
      setDirection(sync ? directionOf(sync) : "inbound");
      setLinkOn(linkOptionOn(sync?.filters));
    }
  }
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
          <OrganizationSharedContextNotice elevation={1} actor={sync ? { mode: "runs", who: sync.owner_email ?? t_i18n("Unknown"), savingAs: meLabel } : null} />
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
                onValueChange={(next) => setDirection(next === "outbound" ? "outbound" : "inbound")}
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
          <SyncScheduleFields idPrefix="org-sync-edit" value={schedule} onChange={setSchedule} />
          {hasLinkOption(sync) && (
            <SettingsSwitchField
              id="org-sync-edit-link-supporting-assets"
              label={t_i18n("Link supporting assets to primary assets")}
              helper={t_i18n("On by default. Off: primary assets are imported without their supporting assets.")}
              info={t_i18n(
                "Links each primary asset to the supporting assets OpenCRQ attaches to it, matched by OpenCRQ id or name. A link you add or remove by hand always wins.",
              )}
              checked={linkOn}
              onCheckedChange={setLinkOn}
            />
          )}
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
          <Button
            onClick={() => {
              // The link option is stored only when off ("false"), as the add dialog stores it.
              const filters =
                hasLinkOption(sync) && linkOn !== linkOptionOn(sync.filters)
                  ? (() => {
                      const { link_supporting_assets: _dropped, ...rest } = sync.filters ?? {};
                      return linkOn ? rest : { ...rest, link_supporting_assets: "false" };
                    })()
                  : undefined;
              onSave(schedule, enabled, sectionSync ? direction : undefined, filters);
            }}
            disabled={saving}
            aria-busy={saving || undefined}
          >
            {saving && <Spinner size="sm" tone="inherit" label={t_i18n("Saving...")} />}
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
