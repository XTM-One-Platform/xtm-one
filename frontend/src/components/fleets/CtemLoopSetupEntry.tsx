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
 */
import { useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

import { CtemLoopSetupDialog } from "./CtemLoopSetupDialog";

/** The slug of the seeded CTEM loop fleet (`builtin_fleets.CTEM_LOOP_SLUG`). */
export const CTEM_LOOP_FLEET_SLUG = "ctem-loop";

/** Whether the entry shows: an administrator, in XTM One mode, not impersonating. */
export function showsCtemLoopSetupEntry(isAdmin: boolean, isXtmOne: boolean, impersonating: boolean): boolean {
  return isAdmin && isXtmOne && !impersonating;
}

export function CtemLoopSetupEntry({ size = "sm" }: { size?: "sm" | "default" }) {
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const [open, setOpen] = useState(false);
  if (!showsCtemLoopSetupEntry(Boolean(isAdmin), Boolean(isXtmOne), Boolean(user?.impersonating))) return null;
  return (
    <>
      <Button type="button" variant="outline" size={size} className="gap-1.5" onClick={() => setOpen(true)} data-testid="ctem-loop-setup-entry">
        <Icon name="network" size={16} aria-hidden />
        {t_i18n("Set up the CTEM loop")}
      </Button>
      {open && <CtemLoopSetupDialog open onOpenChange={setOpen} />}
    </>
  );
}
