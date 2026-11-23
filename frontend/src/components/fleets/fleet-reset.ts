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

/** The lines of a fleet reset's confirmation (#1508), from its dry run. */
import type { FleetResetResult } from "@/hooks/api/useFleetApi";
import type { useFormatter } from "@/i18n";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** One line per thing the reset changes; a count of 0 is left out, so a fleet already reset gives none. */
export function resetPlanLines(plan: FleetResetResult, t_i18n: Translate): string[] {
  const lines: string[] = [];
  if (plan.automations > 0)
    lines.push(t_i18n("{count, plural, one {# automation switched off and back to not set up} other {# automations switched off and back to not set up}}", { count: plan.automations }));
  if (plan.run_as_cleared > 0)
    lines.push(t_i18n("{count, plural, one {# run-as user cleared} other {# run-as users cleared}}", { count: plan.run_as_cleared }));
  if (plan.bindings > 0)
    lines.push(t_i18n("{count, plural, one {# binding removed from the members} other {# bindings removed from the members}}", { count: plan.bindings }));
  if (plan.pins > 0) lines.push(t_i18n("{count, plural, one {# approval pin removed} other {# approval pins removed}}", { count: plan.pins }));
  if (plan.gates > 0) lines.push(t_i18n("{count, plural, one {# added gate removed} other {# added gates removed}}", { count: plan.gates }));
  if (plan.autonomy_level) lines.push(t_i18n("Autonomy level back to the shipped one"));
  if (plan.agents > 0)
    lines.push(t_i18n("{count, plural, one {# member agent rewritten from its shipped definition} other {# member agents rewritten from their shipped definition}}", { count: plan.agents }));
  if (plan.settings.length > 0)
    lines.push(t_i18n("{count, plural, one {# fleet setting back to its default} other {# fleet settings back to their default}}", { count: plan.settings.length }));
  if (plan.documentation) lines.push(t_i18n("Documentation back to the shipped version"));
  return lines;
}

/**
 * The automations an emergency stop holds on the fleet (UX round 2 B1 on #1523): while any
 * is held the reset is refused by the server, so the dialog names them and never reads
 * "already in its factory state".
 */
export function resetHeldByStop(plan: FleetResetResult | undefined): string[] {
  return plan?.held_by_stop ?? [];
}
