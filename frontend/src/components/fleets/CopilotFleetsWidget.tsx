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
 * The Copilot home's Fleets widget wired to its data (#12063): every fleet the
 * person sees by default, their own choice in `users.copilot_dashboard_fleet_ids`
 * otherwise (independent of the command center's `ctem_dashboard_fleet_ids`),
 * the shared chooser, and the lock of Community Edition.
 */
import { useCallback, useState } from "react";

import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useCtemFleetActivity } from "@/hooks/api/useCtemFleetActivity";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useAuth } from "@/hooks/useAuth";
import { useAiLockReason } from "@/hooks/useAiLockReason";
import { useAgenticAIEnabled, usePlatformConfig } from "@/hooks/usePlatformConfig";
import { ChooseFleetsDialog } from "./ChooseFleetsDialog";
import { CopilotFleetsCard } from "./CopilotFleetsCard";

export function CopilotFleetsWidget({ onContentHeight }: { onContentHeight?: (px: number) => void }) {
  const { isAdmin } = useAuth();
  const { data: user } = useMe();
  const updateProfile = useUpdateProfile();
  const configLoaded = !!usePlatformConfig();
  const agenticLicensed = useAgenticAIEnabled();
  const lockReason = useAiLockReason();
  const licensed = configLoaded && agenticLicensed;
  // Optimistic: the widget follows a hide or a save at once.
  const [override, setOverride] = useState<string[] | null | undefined>(undefined);
  const chosenIds = override !== undefined ? override : (user?.copilot_dashboard_fleet_ids ?? null);
  const activity = useCtemFleetActivity({ isAdmin, licensed, chosenIds, fallback: "all", withTimeline: false });
  const { data: allFleets } = useFleets({ optional: true, enabled: licensed });
  const [chooserOpen, setChooserOpen] = useState(false);
  const save = useCallback(
    (ids: string[] | null) => {
      setOverride(ids);
      updateProfile.mutate({ copilot_dashboard_fleet_ids: ids });
    },
    [updateProfile],
  );
  const hide = useCallback(
    (tile: { id: string }) => save(activity.tiles.map((t) => t.id).filter((id) => id !== tile.id)),
    [save, activity.tiles],
  );

  return (
    <>
      <CopilotFleetsCard
        activity={activity}
        isLoading={activity.isLoading || !configLoaded}
        licensed={!configLoaded || agenticLicensed}
        lockReason={lockReason}
        canSetUp={isAdmin}
        ordered={chosenIds != null}
        noFleet={!!allFleets && allFleets.every((f) => f.is_archived)}
        onChooseFleets={() => setChooserOpen(true)}
        onHideFleet={hide}
        onContentHeight={onContentHeight}
      />
      {allFleets && (
        <ChooseFleetsDialog
          open={chooserOpen}
          onOpenChange={setChooserOpen}
          fleets={allFleets}
          chosenIds={chosenIds}
          onSave={save}
          fallback="all"
        />
      )}
    </>
  );
}
