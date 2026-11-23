// SPDX-License-Identifier: Apache-2.0

/**
 * Removing rows a platform sync owns (#1134): the list editor asks first, because the next
 * run of their sync brings them back. The dialog names the source and the next run, and
 * offers "Exclude from this sync": the source ids then go on the sync's exclusion list
 * (`excluded_external_ids`, visible and reversible on the sync) when the list is saved, so a
 * removal the person undoes or cancels never excludes anything.
 */
import { useCallback, useId, useRef, useState } from "react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SettingsSwitchField } from "@/components/ui/settings-form";
import { type OrganizationSync, useOrganizationSyncs, useUpdateOrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { PLATFORM_NAMES } from "@/lib/organization-profile";
import { exclusionsBySync, isPlatformSource, sourcePlatform, syncedCount, type Provenance } from "@/lib/organization-provenance";
import { timeUntil } from "@/lib/utils";

interface Ask {
  values: Provenance[];
  resolve: (confirmed: boolean) => void;
}

/** `enabled` false for a reader, who removes no row and may not read the syncs (UX r1 B1 on #1540). */
export function useSyncedRemoval(enabled = true) {
  const { t_i18n, locale } = useFormatter();
  const switchId = useId();
  const { data: syncs = [] } = useOrganizationSyncs("", enabled);
  const update = useUpdateOrganizationSync();
  const [ask, setAsk] = useState<Ask | null>(null);
  const [exclude, setExclude] = useState(false);
  // The rows to exclude when the list is saved, by sync id.
  const pending = useRef<Map<string, Set<string>>>(new Map());
  // ConfirmDialog calls onConfirm then onOpenChange(false): the second call finds the ask answered.
  const answered = useRef(false);

  const syncOf = useCallback((id: string | undefined): OrganizationSync | undefined => (id ? syncs.find((sync) => sync.id === id) : undefined), [syncs]);

  /** The lock tip of a synced row: its platform and its sync, or null for a hand or agent row. */
  const lockTip = useCallback(
    (value: Provenance): string | null => {
      if (!isPlatformSource(value.source)) return null;
      const platform = PLATFORM_NAMES[sourcePlatform(value.source)] ?? value.source ?? "";
      return syncOf(value.sync_id)
        ? t_i18n("From {platform}, kept in sync: only the local fields can be edited here.", { platform })
        : t_i18n("From {platform}: only the local fields can be edited here.", { platform });
    },
    [syncOf, t_i18n],
  );

  /** Asks before synced rows are removed; resolves false when the person keeps them. */
  const confirmRemove = useCallback(
    (values: Provenance[]) =>
      new Promise<boolean>((resolve) => {
        setExclude(false);
        answered.current = false;
        setAsk({ values: values.filter((value) => isPlatformSource(value.source)), resolve });
      }),
    [],
  );

  const close = (confirmed: boolean) => {
    if (!ask || answered.current) return;
    answered.current = true;
    if (confirmed && exclude) {
      for (const [syncId, ids] of exclusionsBySync(ask.values)) {
        const set = pending.current.get(syncId) ?? new Set<string>();
        ids.forEach((id) => set.add(id));
        pending.current.set(syncId, set);
      }
    }
    ask.resolve(confirmed);
    setAsk(null);
  };

  /** After a save: writes the exclusions of the rows the saved list no longer holds. */
  const commit = useCallback(
    (saved: readonly Provenance[]) => {
      const kept = new Set(saved.map((value) => value.external_id).filter(Boolean));
      for (const [syncId, ids] of pending.current) {
        const sync = syncOf(syncId);
        const added = [...ids].filter((id) => !kept.has(id));
        if (!sync || added.length === 0) continue;
        update.mutate({
          id: sync.id,
          body: {
            section: sync.section,
            source: sync.source,
            filters: sync.filters,
            batch_type: sync.batch_type,
            remove_gone: sync.remove_gone,
            excluded_external_ids: [...new Set([...(sync.excluded_external_ids ?? []), ...added])],
            frequency: sync.frequency,
            hour: sync.hour,
            minute: sync.minute,
            weekday: sync.weekday,
            timezone: sync.timezone,
            enabled: sync.enabled,
          },
        });
      }
      pending.current = new Map();
    },
    [syncOf, update],
  );

  const reset = useCallback(() => {
    pending.current = new Map();
  }, []);

  const values = ask?.values ?? [];
  const count = syncedCount(values);
  const platforms = [...new Set(values.map((value) => PLATFORM_NAMES[sourcePlatform(value.source)] ?? value.source ?? ""))].join(", ");
  const nextRuns = [...new Set(values.map((value) => syncOf(value.sync_id)?.next_run_at).filter((at): at is string => !!at))].sort();
  const excludable = exclusionsBySync(values).size > 0;

  const element = (
    <ConfirmDialog
      open={ask !== null}
      onOpenChange={(open) => {
        if (!open) close(false);
      }}
      variant="warning"
      title={t_i18n("{count, plural, one {Remove # synced row?} other {Remove # synced rows?}}", { count })}
      description={
        nextRuns.length > 0
          ? t_i18n(
              "{count, plural, one {It comes from {platform}: the next run of its sync, {when}, brings it back unless you exclude it.} other {They come from {platform}: the next run of their sync, {when}, brings them back unless you exclude them.}}",
              { count, platform: platforms, when: timeUntil(nextRuns[0], locale) },
            )
          : t_i18n("{count, plural, one {It comes from {platform}: the next import brings it back unless you exclude it.} other {They come from {platform}: the next import brings them back unless you exclude them.}}", { count, platform: platforms })
      }
      confirmLabel={t_i18n("Remove")}
      onConfirm={() => close(true)}
    >
      {excludable && (
        <SettingsSwitchField
          id={switchId}
          label={t_i18n("Exclude from this sync")}
          helper={t_i18n("{count, plural, one {The sync never imports it again. You can take it out of its exclusions later.} other {The sync never imports them again. You can take them out of its exclusions later.}}", { count })}
          checked={exclude}
          onCheckedChange={setExclude}
        />
      )}
    </ConfirmDialog>
  );

  return { element, lockTip, confirmRemove, commit, reset };
}
