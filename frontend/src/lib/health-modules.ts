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

/** Platform Health modules on the Health page (#101 S8): names and card copy. */
import type { HealthModule, HealthModuleCard } from "@/types";

/** Display names of the modules (catalog keys). */
export const HEALTH_MODULE_LABELS: Record<HealthModule, string> = {
  agents: "Agents",
  fleets: "Fleets",
  xtm_one: "XTM One",
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
};

/** The card's subtitle: open counts by severity, then the week's change when known. */
export function moduleCardSubtitle(
  card: HealthModuleCard,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  const counts = t("{critical} critical, {warning} warning, {info} info", {
    critical: card.critical,
    warning: card.warning,
    info: card.info,
  });
  if (card.previous_score === null) return counts;
  const delta = card.score - card.previous_score;
  const trend = delta === 0 ? t("unchanged in 7 days") : t("{delta} in 7 days", { delta: delta > 0 ? `+${delta}` : String(delta) });
  return `${counts} · ${trend}`;
}
