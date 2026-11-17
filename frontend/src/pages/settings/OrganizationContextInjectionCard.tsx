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
 * Settings > Organization: "Use organization context in every agent" (#11533).
 * The platform default of the cascade assignment > agent > platform: on, every
 * agent whose mode is "Inherit" starts its prompt with the basic block (the
 * identity and an index of the profile's categories); the categories chosen
 * here are preloaded after it for every agent that inherits them. An agent or
 * an assignment overrides either. Read view at rest, Edit opens the fields,
 * Save or Cancel returns (13.au).
 */
import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { MultiSelect } from "@/components/ui/multi-select";
import {
  SettingsCard,
  SettingsField,
  SettingsSaveBar,
  SettingsSwitchField,
  settingsFieldHintId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useOrganizationContextInjection, useSaveOrganizationContextInjection } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { ORGANIZATION_CONTEXT_CATEGORY_LABELS } from "@/lib/constants";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="file-text" size={16} />
    </span>
  );
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Settings > Organization is administrators only, like the PUT it calls. */
export function OrganizationContextInjectionCard() {
  const { t_i18n } = useFormatter();
  const id = useId();
  const { data } = useOrganizationContextInjection();
  const save = useSaveOrganizationContextInjection();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(true);
  const [draftCategories, setDraftCategories] = useState<string[]>([]);
  const enabled = data?.enabled ?? true;
  const categories = data?.categories ?? [];
  const available = data?.available_categories ?? [];
  const label = (name: string) => {
    const known = ORGANIZATION_CONTEXT_CATEGORY_LABELS[name as keyof typeof ORGANIZATION_CONTEXT_CATEGORY_LABELS];
    return known ? t_i18n(known) : name;
  };
  const info = t_i18n(
    "On: every agent whose organization context is Inherit starts each chat and run with a short block: the organization's name, sector, regions, size and risk appetite, then one line per category of the profile with its size and the get_organization_context call that reads it. An agent or an assignment set to Always or Off overrides it; Off also removes the tool. Off here: no block unless an agent or an assignment asks for it.",
  );
  const categoriesInfo = t_i18n(
    "Loaded into the prompt after the short block, capped, for every agent that inherits this list; every other category stays one tool call away. An agent or an assignment may choose its own.",
  );

  return (
    <SettingsCard
      data-testid="settings.organization.injection"
      icon={CardIcon}
      title={t_i18n("Organization context in agents")}
      description={t_i18n("The default for every agent and assignment; each can override it.")}
      action={
        !editing ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            aria-label={t_i18n("Edit organization context in agents")}
            onClick={() => {
              setDraft(enabled);
              setDraftCategories(categories);
              setEditing(true);
            }}
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {!editing ? (
        <SettingsReadGrid>
          <SettingsReadField
            label={t_i18n("Use organization context in every agent")}
            info={info}
            value={enabled ? t_i18n("On") : t_i18n("Off")}
          />
          <SettingsReadField
            label={t_i18n("Preloaded organization categories")}
            info={categoriesInfo}
            value={categories.length ? categories.map(label).join(", ") : t_i18n("None")}
          />
        </SettingsReadGrid>
      ) : (
        <div className="space-y-3">
          <SettingsSwitchField
            className="rounded-sm bg-elevation-default p-3.5"
            id={id}
            label={t_i18n("Use organization context in every agent")}
            helper={t_i18n("Agents set to Inherit follow this switch.")}
            info={info}
            checked={draft}
            onCheckedChange={setDraft}
          />
          <div data-testid="settings.organization.injection.categories">
            <SettingsField
              id={`${id}-categories`}
              label={t_i18n("Preloaded organization categories")}
              info={categoriesInfo}
              helper={t_i18n("Empty: nothing is preloaded, every category stays one get_organization_context call away.")}
            >
              <MultiSelect
                id={`${id}-categories`}
                aria-label={t_i18n("Preloaded organization categories")}
                aria-describedby={settingsFieldHintId(`${id}-categories`)}
                options={available.map((name) => ({ value: name, label: label(name) }))}
                selected={draftCategories}
                onToggle={(name) =>
                  setDraftCategories((current) =>
                    current.includes(name) ? current.filter((v) => v !== name) : [...current, name],
                  )
                }
                placeholder={t_i18n("None")}
                searchPlaceholder={t_i18n("Filter")}
                emptyOptionsLabel={t_i18n("No category")}
                noMatchLabel={t_i18n("No category")}
                sort={false}
              />
            </SettingsField>
          </div>
          <SettingsSaveBar
            dirty={draft !== enabled || !sameList(draftCategories, categories)}
            saving={save.isPending}
            onSave={() =>
              save.mutate({ enabled: draft, categories: draftCategories }, { onSuccess: () => setEditing(false) })
            }
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </SettingsCard>
  );
}
