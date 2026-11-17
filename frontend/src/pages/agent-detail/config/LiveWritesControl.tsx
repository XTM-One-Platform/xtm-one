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

import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";

/** The providers whose bindings carry a Live writes override (backend LIVE_WRITES_PROVIDERS). */
export const LIVE_WRITES_PROVIDERS: readonly string[] = ["opencti"];

/** A binding's override: absent = inherit the integration's setting. */
export type LiveWritesMode = "inherit" | "on" | "off";

/** The integration-level setting as stored in its extra config; anything but "on" reads Off. */
export function integrationLiveWrites(extraConfig: Record<string, unknown> | null | undefined): boolean {
  return String(extraConfig?.live_writes ?? "").trim().toLowerCase() === "on";
}

/**
 * The overrides an agent save sends: only the keys still bound (an integration id of an
 * OpenCTI instance binding, or "opencti" for its per-user binding), keys sorted so the
 * diff against the stored value is stable; null when none is left.
 */
export function prunedLiveWrites(
  draft: Record<string, "on" | "off">,
  bindings: Record<string, string[]>,
  templateBindings: string[],
): Record<string, "on" | "off"> | null {
  const bound = new Set<string>([
    ...Object.entries(bindings)
      .filter(([provider]) => LIVE_WRITES_PROVIDERS.includes(provider.split("#")[0]))
      .flatMap(([, ids]) => ids),
    ...templateBindings.filter((p) => LIVE_WRITES_PROVIDERS.includes(p)),
  ]);
  const kept = Object.keys(draft)
    .filter((key) => bound.has(key))
    .sort()
    .map((key) => [key, draft[key]] as const);
  return kept.length ? Object.fromEntries(kept) : null;
}

/**
 * The Writes control of one bound OpenCTI integration on the agent's Tooling tab:
 * Inherit (the integration's own Live writes setting, named), Live, or Draft only.
 * Turning a binding to Live needs the authority to edit that integration, as the
 * integration-level field does; Draft only and Inherit need only the agent.
 */
export function LiveWritesControl({
  bindingKey,
  label,
  inheritedLive,
  value,
  canSetLive,
  onChange,
}: {
  /** The integration id, or the provider key of a per-user binding. */
  bindingKey: string;
  /** The integration's name, for the control's accessible name. */
  label: string;
  /** The integration's own setting; null when a per-user binding resolves it at runtime. */
  inheritedLive: boolean | null;
  value: LiveWritesMode;
  canSetLive: boolean;
  onChange: (mode: LiveWritesMode) => void;
}) {
  const { t_i18n } = useFormatter();
  const inheritDescription = inheritedLive === null
    ? t_i18n("Follows each user's integration")
    : inheritedLive
      ? t_i18n("Follows the integration, currently Live")
      : t_i18n("Follows the integration, currently Draft only");
  const options = [
    { value: "inherit", label: t_i18n("Inherit"), description: inheritDescription },
    {
      value: "on",
      label: t_i18n("Live"),
      description: canSetLive ? t_i18n("Writes go to the live graph, no review") : t_i18n("Needs an administrator or the integration's manager"),
      disabled: !canSetLive && value !== "on",
    },
    { value: "off", label: t_i18n("Draft only"), description: t_i18n("Writes are staged in a draft for review") },
  ];
  const helpId = `live-writes-help-${bindingKey}`;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="content-caption shrink-0 text-default-secondary">{t_i18n("Writes")}</span>
        <SearchableSelect
          aria-label={t_i18n("Writes: {integration}", { integration: label })}
          aria-describedby={helpId}
          searchable={false}
          sort={false}
          options={options}
          value={value}
          onValueChange={(v) => onChange(v as LiveWritesMode)}
          className="min-w-0 max-w-56 flex-1"
        />
      </div>
      <p id={helpId} className="content-caption text-default-secondary">
        {value === "inherit" ? inheritDescription : value === "on" ? t_i18n("Writes go to the live graph, no review") : t_i18n("Writes are staged in a draft for review")}
      </p>
    </div>
  );
}

/** The read view of an override: nothing when the binding inherits. */
export function LiveWritesBadge({ mode }: { mode: "on" | "off" | undefined }) {
  const { t_i18n } = useFormatter();
  if (mode !== "on" && mode !== "off") return null;
  return (
    <Badge variant="outline" className="shrink-0">
      {mode === "on" ? t_i18n("Writes: Live") : t_i18n("Writes: Draft only")}
    </Badge>
  );
}
