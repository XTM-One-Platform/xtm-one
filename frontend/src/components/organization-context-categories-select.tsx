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
 * The organization context categories an agent or an assignment preloads
 * into its prompt after the basic block (#11533): Inherit (null, the agent's
 * then the platform's list) or its own list (empty: none), with the list in
 * effect and where it comes from, read from the cascade assignment > agent >
 * platform (`organizationContextCategoriesEffective`, the mirror of the
 * backend resolver). A category not preloaded stays one tool call away.
 */
import { useId } from "react";

import { MultiSelect } from "@/components/ui/multi-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useOrganizationContextInjection } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { ORGANIZATION_CONTEXT_CATEGORY_LABELS, organizationContextCategoriesEffective } from "@/lib/constants";

/** The category label a person reads ("Software"), the name itself for a category the frontend does not know yet. */
function useCategoryLabel(): (name: string) => string {
  const { t_i18n } = useFormatter();
  return (name) => {
    const label = ORGANIZATION_CONTEXT_CATEGORY_LABELS[name as keyof typeof ORGANIZATION_CONTEXT_CATEGORY_LABELS];
    return label ? t_i18n(label) : name;
  };
}

/** The sentence under the field and in the read view: "Software, Crown jewels, from the agent", "None, set here"... */
export function useOrganizationContextCategoriesLabel(
  scope: "agent" | "assignment",
  value: string[] | null | undefined,
  agentValue?: string[] | null,
): string {
  const { t_i18n } = useFormatter();
  const label = useCategoryLabel();
  const { data } = useOrganizationContextInjection();
  const platform = data?.categories ?? [];
  const effective =
    scope === "agent"
      ? organizationContextCategoriesEffective(platform, value)
      : organizationContextCategoriesEffective(platform, agentValue, value);
  const source =
    value != null ? "here" : scope === "assignment" && agentValue != null ? "agent" : "platform";
  const names = effective.length ? effective.map(label).join(", ") : t_i18n("None");
  if (source === "here") return t_i18n("{names}, set here", { names });
  if (source === "agent") return t_i18n("{names}, from the agent", { names });
  return t_i18n("{names}, from the platform", { names });
}

export function OrganizationContextCategoriesSelect({
  scope,
  value,
  agentValue,
  onChange,
}: {
  scope: "agent" | "assignment";
  /** null: inherit. */
  value: string[] | null;
  /** The agent's own list, for an assignment: what Inherit falls back to first. */
  agentValue?: string[] | null;
  onChange: (value: string[] | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const id = useId();
  const label = useCategoryLabel();
  const { data } = useOrganizationContextInjection();
  const available = data?.available_categories ?? [];
  const effective = useOrganizationContextCategoriesLabel(scope, value, agentValue);
  const selected = value ?? [];
  return (
    <div data-testid={`${scope}.organization-context-categories`}>
      <SettingsField
        id={id}
        label={t_i18n("Preloaded organization categories")}
        info={t_i18n(
          "Categories of the organization profile loaded into the prompt after the short block, for example Software or Crown jewels. Inherit follows the agent, then the platform list in Settings > Organization; Choose sets this list, and an empty list preloads nothing: every category stays one get_organization_context call away.",
        )}
        helper={t_i18n(
          "Categories of the profile loaded into the prompt; every other one stays one get_organization_context call away. In effect: {effective}.",
          { effective },
        )}
      >
        <div className="space-y-2">
          <Select value={value == null ? "inherit" : "custom"} onValueChange={(v) => onChange(v === "inherit" ? null : selected)}>
            <SelectTrigger id={id} aria-describedby={settingsFieldHintId(id)}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="inherit">{t_i18n("Inherit")}</SelectItem>
              <SelectItem value="custom">{t_i18n("Choose")}</SelectItem>
            </SelectContent>
          </Select>
          {value != null && (
            <MultiSelect
              id={`${id}-list`}
              aria-label={t_i18n("Preloaded organization categories")}
              aria-describedby={settingsFieldHintId(id)}
              options={available.map((name) => ({ value: name, label: label(name) }))}
              selected={selected}
              onToggle={(name) => onChange(selected.includes(name) ? selected.filter((v) => v !== name) : [...selected, name])}
              placeholder={t_i18n("None")}
              searchPlaceholder={t_i18n("Filter")}
              emptyOptionsLabel={t_i18n("No category")}
              noMatchLabel={t_i18n("No category")}
              sort={false}
            />
          )}
        </div>
      </SettingsField>
    </div>
  );
}
