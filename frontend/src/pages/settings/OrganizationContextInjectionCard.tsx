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
 * identity and an index of the profile's categories). No category is preloaded
 * any more (#2443): an agent queries the profile and the inventory with its
 * tools. An agent or an assignment overrides the switch. Read view at rest, Edit opens the fields,
 * Save or Cancel returns (13.au).
 */
import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { SettingsCard, SettingsSaveBar, SettingsSwitchField } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useOrganizationContextInjection, useSaveOrganizationContextInjection } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="file-text" size={16} />
    </span>
  );
}

/** Settings > Organization is administrators only, like the PUT it calls. */
export function OrganizationContextInjectionCard() {
  const { t_i18n } = useFormatter();
  const id = useId();
  const { data } = useOrganizationContextInjection();
  const save = useSaveOrganizationContextInjection();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(true);
  const enabled = data?.enabled ?? true;
  const info = t_i18n(
    "On: every agent whose organization context is Inherit starts each chat and run with a short block: the organization's name, sector, regions, size and risk appetite, then one line per category of the profile with its size and the get_organization_context call that reads it. An agent or an assignment set to Always or Off overrides it; Off also removes the tool. Off here: no block unless an agent or an assignment asks for it.",
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
            wide
            label={t_i18n("Use organization context in every agent")}
            info={info}
            value={enabled ? t_i18n("On") : t_i18n("Off")}
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
          <SettingsSaveBar
            dirty={draft !== enabled}
            saving={save.isPending}
            onSave={() =>
              save.mutate({ enabled: draft }, { onSuccess: () => setEditing(false) })
            }
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </SettingsCard>
  );
}
