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

import { Lock } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { isFleetsUnavailable, useFleetMemberPresets } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { FleetMemberPresetsSection } from "./FleetMemberPresetsSection";

/**
 * Settings > Agents and automations > Fleets: the member presets alone. The
 * fleets themselves are listed on the Fleets page and in Objects, so this
 * section never lists them again (Sam, 12 Nov 2026). The presets are read by
 * every signed-in user and gated by the agentic AI licence, so the licence
 * refusal (403) is said as on the Fleets list; a preset never needs a fleet.
 */
export function FleetsSettingsSection() {
  const { t_i18n } = useFormatter();
  // The same query the presets table reads: one request, its refusal said here.
  const { error } = useFleetMemberPresets();

  if (isFleetsUnavailable(error)) {
    return (
      <EmptyState
        icon={Lock}
        title={t_i18n("Fleets need the agentic AI licence")}
        description={t_i18n("A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept. Fleets are part of the agentic AI features of the Enterprise Edition.")}
      />
    );
  }

  return <FleetMemberPresetsSection />;
}
