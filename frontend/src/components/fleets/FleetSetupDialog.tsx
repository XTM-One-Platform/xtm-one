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
 * The one setup of a fleet (#1151), behind the card's "Set up", the header's
 * primary action and "Run setup again": the CTEM loop fleet opens its
 * versioned guided setup (`CtemLoopSetupDialog`, #1087) for an administrator
 * of XTM One, every other fleet (and the CTEM loop fleet for anyone else) the
 * fleet setup wizard. Nothing is enabled before the wizard's Confirm.
 */
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";

import { CtemLoopSetupDialog } from "./CtemLoopSetupDialog";
import { CTEM_LOOP_FLEET_SLUG, showsCtemLoopSetupEntry } from "./CtemLoopSetupEntry";
import { FleetSetupWizard } from "./FleetSetupWizard";

/** Whether a fleet opens the CTEM loop's own guided setup rather than the generic wizard. */
function usesCtemLoopSetup(slug: string | null | undefined, isAdmin: boolean, isXtmOne: boolean, impersonating: boolean): boolean {
  return slug === CTEM_LOOP_FLEET_SLUG && showsCtemLoopSetupEntry(isAdmin, isXtmOne, impersonating);
}

export function FleetSetupDialog({
  fleetId,
  fleetName,
  fleetSlug,
  open,
  onOpenChange,
}: {
  fleetId: string;
  fleetName: string;
  fleetSlug?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  if (!open) return null;
  if (usesCtemLoopSetup(fleetSlug, Boolean(isAdmin), Boolean(isXtmOne), Boolean(user?.impersonating))) {
    return <CtemLoopSetupDialog open onOpenChange={onOpenChange} />;
  }
  return <FleetSetupWizard fleetId={fleetId} fleetName={fleetName} open onOpenChange={onOpenChange} />;
}
