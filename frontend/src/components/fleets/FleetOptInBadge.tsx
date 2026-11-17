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
 * Whether a fleet is set up (#9566), as one badge: Not set up, Partially on,
 * On, Paused. Shared by the fleet page header (#9769 S2) and the CTEM fleet
 * activity tiles, so the two never say it differently.
 */
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import type { FleetOptIn } from "@/lib/fleet-activity";

/** The English label of a fleet's opt-in state. Own keys, never the generic
 *  "On" / "Paused": the fleet is feminine in fr and es (UX round 2 on #9570, B2). @i18n-keys */
const FLEET_OPT_IN_LABELS = {
  not_set_up: "Not set up",
  partial: "Partially on",
  on: "On (fleet opt-in)",
  paused: "Paused (fleet opt-in)",
} as const satisfies Record<FleetOptIn, string>;

const FLEET_OPT_IN_VARIANTS: Record<FleetOptIn, "success" | "warning" | "outline" | "secondary"> = {
  not_set_up: "outline",
  partial: "secondary",
  on: "success",
  paused: "warning",
};

export function FleetOptInBadge({ optIn, className }: { optIn: FleetOptIn; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant={FLEET_OPT_IN_VARIANTS[optIn]} className={className ?? "shrink-0"} data-testid="fleet-opt-in-badge" data-state={optIn}>
      {t_i18n(FLEET_OPT_IN_LABELS[optIn])}
    </Badge>
  );
}
