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
 * "Set up the CTEM loop" (#1087, slice 4): the guided setup is always
 * reachable again, from the command center and the CTEM loop fleet page, for
 * administrators of XTM One only (the status endpoint answers 403 to anyone
 * else). It opens the same `CtemLoopSetupDialog` on the step to resume on.
 *
 * On the command center it is a nudge (`dashboardSetupEntryShown`): the page
 * draws it only while no CTEM work runs, and passes `onDismiss` for the close
 * control that hides it for good for that person.
 */
import { useCallback, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useCtemLoopSetupStatus } from "@/hooks/api/useFleetApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

import { CtemLoopSetupDialog } from "./CtemLoopSetupDialog";
import { SETUP_ENTRY_DISMISS_KEY, dashboardSetupEntryShown } from "./ctem-loop-setup";

/** The slug of the seeded CTEM loop fleet (`builtin_fleets.CTEM_LOOP_SLUG`). */
export const CTEM_LOOP_FLEET_SLUG = "ctem-loop";

/** Whether the entry shows: an administrator, in XTM One mode, not impersonating. */
export function showsCtemLoopSetupEntry(isAdmin: boolean, isXtmOne: boolean, impersonating: boolean): boolean {
  return isAdmin && isXtmOne && !impersonating;
}

/**
 * The command center's setup entry (Sam, 23 Nov 2026): drawn only while no CTEM
 * work runs and the person has not closed it. *fleetAutomationsOn* is the
 * automations on of each fleet the page shows, `null` while the fleets load.
 * Closing writes `entry:ctem-loop-setup` into the person's dismissed notices
 * (`users.organization_context_notice_dismissed`), every device, for good.
 */
export function useDashboardSetupEntry(fleetAutomationsOn: readonly (number | null | undefined)[] | null) {
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const updateProfile = useUpdateProfile();
  const canSetUp = showsCtemLoopSetupEntry(Boolean(isAdmin), Boolean(isXtmOne), Boolean(user?.impersonating));
  const dismissed = user?.organization_context_notice_dismissed ?? null;
  const status = useCtemLoopSetupStatus(canSetUp && !dismissed?.includes(SETUP_ENTRY_DISMISS_KEY));
  const shown = dashboardSetupEntryShown({
    canSetUp,
    status: status.data,
    statusLoading: status.isLoading,
    fleetAutomationsOn,
    dismissed,
  });
  const dismiss = useCallback(
    () =>
      // Whole-field write; `useUpdateProfile` patches `["me"]` optimistically,
      // so the entry goes at once and comes back only if the round-trip fails.
      updateProfile.mutate({
        organization_context_notice_dismissed: [...new Set([...(dismissed ?? []), SETUP_ENTRY_DISMISS_KEY])],
      }),
    [updateProfile, dismissed],
  );
  return { shown, dismiss };
}

export function CtemLoopSetupEntry({ size = "sm", onDismiss }: { size?: "sm" | "default"; onDismiss?: () => void }) {
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const [open, setOpen] = useState(false);
  if (!showsCtemLoopSetupEntry(Boolean(isAdmin), Boolean(isXtmOne), Boolean(user?.impersonating))) return null;
  const entry = (
    <Button type="button" variant="outline" size={size} className="gap-1.5" onClick={() => setOpen(true)} data-testid="ctem-loop-setup-entry">
      <Icon name="network" size={16} aria-hidden />
      {t_i18n("Set up the CTEM loop")}
    </Button>
  );
  return (
    <>
      {onDismiss ? (
        <span className="inline-flex items-center gap-1">
          {entry}
          <Tip content={t_i18n("Hide from the command center. The setup stays on the CTEM loop fleet page.")}>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={onDismiss}
              aria-label={t_i18n("Hide the CTEM loop setup from the command center")}
              data-testid="ctem-loop-setup-entry-dismiss"
            >
              <Icon name="x" size={16} aria-hidden />
            </Button>
          </Tip>
        </span>
      ) : (
        entry
      )}
      {open && <CtemLoopSetupDialog open onOpenChange={setOpen} />}
    </>
  );
}
