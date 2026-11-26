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
 * The organization context mode of an agent or an assignment (#11533):
 * Inherit, Always or Off, with the value in effect and where it comes from,
 * read from the cascade assignment > agent > platform
 * (`organizationContextEffective`, the mirror of the backend resolver).
 */
import { useId } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useOrganizationContextInjection } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { ORGANIZATION_CONTEXT_MODES, ORGANIZATION_CONTEXT_MODE_LABELS, organizationContextEffective } from "@/lib/constants";
import type { OrganizationContextMode } from "@/types";

/** The sentence under the field and in the read view: "On, from the platform", "Off, set here"... */
export function useOrganizationContextEffectiveLabel(
  scope: "agent" | "assignment",
  mode: OrganizationContextMode,
  agentMode?: OrganizationContextMode,
): string {
  const { t_i18n } = useFormatter();
  const { data } = useOrganizationContextInjection();
  const platform = data?.enabled ?? true;
  const on =
    scope === "agent" ? organizationContextEffective(platform, mode) : organizationContextEffective(platform, agentMode, mode);
  const source =
    mode !== "inherit"
      ? "here"
      : scope === "assignment" && agentMode && agentMode !== "inherit"
        ? "agent"
        : "platform";
  if (source === "here") return on ? t_i18n("On, set here") : t_i18n("Off, set here");
  if (source === "agent") return on ? t_i18n("On, from the agent") : t_i18n("Off, from the agent");
  return on ? t_i18n("On, from the platform") : t_i18n("Off, from the platform");
}

export function OrganizationContextModeSelect({
  scope,
  value,
  agentMode,
  onChange,
}: {
  scope: "agent" | "assignment";
  value: OrganizationContextMode;
  /** The agent's own mode, for an assignment: what Inherit falls back to first. */
  agentMode?: OrganizationContextMode;
  onChange: (mode: OrganizationContextMode) => void;
}) {
  const { t_i18n } = useFormatter();
  const id = useId();
  const effective = useOrganizationContextEffectiveLabel(scope, value, agentMode);
  return (
    <div data-testid={`${scope}.organization-context-mode`}>
      <SettingsField
        id={id}
        label={t_i18n("Organization context")}
        info={t_i18n(
          "A short block at the top of the prompt: the organization's identity and one line per category of its profile. Inherit follows the agent, then the platform setting in Settings > Organization; Always forces it on; Off removes the block and the get_organization_context tool.",
        )}
        helper={t_i18n("A short summary of the organization profile at the top of the prompt. In effect: {effective}.", { effective })}
      >
        <Select value={value} onValueChange={(v) => onChange(v as OrganizationContextMode)}>
          <SelectTrigger id={id} className="w-full" aria-describedby={settingsFieldHintId(id)}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ORGANIZATION_CONTEXT_MODES.map((mode) => (
              <SelectItem key={mode} value={mode}>
                {t_i18n(ORGANIZATION_CONTEXT_MODE_LABELS[mode])}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsField>
    </div>
  );
}
