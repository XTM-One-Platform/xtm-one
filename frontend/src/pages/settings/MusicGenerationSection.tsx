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
 * Settings > AI models > Music generation: the provider and model the
 * Video Studio's AI tracks are composed with, beside Video and Speech
 * generation and built the same way. The models are discovered from the
 * configured providers (`/settings/available-music-models`): Eleven Music on
 * ElevenLabs, Lyria on Gemini. "Auto" leaves the choice to the platform: the
 * first provider with a built-in music model. The built-in tracks need none.
 * Opening the picker asks the providers again.
 */
import { useEffect, useId, useState } from "react";
import { SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import { Check, Info, Music, Pencil } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { ModelListRefreshing, ModelSelect } from "@/components/ui/model-select";
import { useAvailableMusicModels, useUpdateGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { modelChoiceValue, parseModelChoice, providerDisplayName } from "@/lib/utils";
import type { AvailableModel, GeneralAssistantConfig, LLMProvider } from "@/types";

export function MusicGenerationSection({
  gaConfig,
  providers,
  isAdmin,
  canAddProviders,
  envLocked,
}: {
  gaConfig: GeneralAssistantConfig | undefined;
  providers: LLMProvider[] | undefined;
  isAdmin: boolean;
  canAddProviders: boolean;
  /** Set by `DEFAULT_MUSIC_PROVIDER`: the environment re-applies it on every restart. */
  envLocked: boolean;
}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const { data: musicModels, isFetching: refreshing, refetch: refreshModels } = useAvailableMusicModels();
  const updateGAConfig = useUpdateGeneralAssistantConfig();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  // The lock arrives with the config, after a Configure click may have opened the form.
  useEffect(() => {
    if (envLocked) setEditing(false);
  }, [envLocked]);
  const options: AvailableModel[] = musicModels ?? [];
  const current = gaConfig?.music_model || null;
  const configuredProvider = providerDisplayName(
    gaConfig?.music_provider ?? options.find((m) => m.model === current)?.provider ?? "",
    gaConfig?.music_provider_id ?? null,
    providers,
  );
  const fallbackProvider = !current && options[0] ? providerDisplayName(options[0].provider, options[0].provider_id ?? null, providers) : null;

  const save = () => {
    const { model, providerId } = parseModelChoice(draft || "");
    const matched = model
      ? options.find((m) => m.model === model && (m.provider_id ?? null) === providerId) ?? options.find((m) => m.model === model)
      : undefined;
    updateGAConfig.mutate(
      {
        // "Auto" clears the choice: the platform then takes the first provider with a built-in music model.
        music_provider: model ? matched?.provider ?? "" : "",
        music_provider_id: model ? providerId ?? matched?.provider_id ?? null : null,
        music_model: model || "",
      },
      { onSuccess: () => setEditing(false) },
    );
  };

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <SettingsGroupTitle icon={Music}>{t_i18n("Music Generation")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n(
              "Used by the Video Studio to compose AI music tracks from a brief: Eleven Music (ElevenLabs) and Lyria (Gemini). The built-in music library needs no provider.",
            )}
          </SettingsGroupDescription>
        </div>
        <div className="ml-4 flex shrink-0 gap-2">
          {envLocked ? (
            <EnvLockBadge />
          ) : isAdmin && (options.length > 0 || current) ? (
            !editing ? (
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                onClick={() => {
                  setDraft(modelChoiceValue(gaConfig?.music_model, gaConfig?.music_provider_id));
                  setEditing(true);
                  void refreshModels();
                }}
              >
                <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
              </Button>
            ) : null
          ) : null}
        </div>
      </div>
      {options.length > 0 || current ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                <Music className="h-4 w-4 text-accent" />
              </div>
              <div>
                <p className="content-base font-medium">{current ?? t_i18n("Auto (provider default)")}</p>
                <span className="content-compact text-default-secondary">
                  {current
                    ? t_i18n("Music Provider: {provider}", { provider: configuredProvider })
                    : fallbackProvider
                      ? t_i18n("Tracks use the first provider with a built-in music model, such as {provider}.", {
                          provider: fallbackProvider,
                        })
                      : t_i18n('Click "Configure" to pick a default music model')}
                </span>
              </div>
            </div>
            {current && (
              <Badge variant="success" className="content-caption">
                <Check className="mr-1 h-3 w-3" />
                {t_i18n("Active")}
              </Badge>
            )}
          </div>
          {editing && (
            <div className="space-y-2">
              <label htmlFor={`${fieldId}-music-model`} className="content-base font-medium text-default-primary">
                {t_i18n("Music Generation Model")}
              </label>
              <p id={`${fieldId}-music-model-hint`} className="content-compact text-default-secondary">
                {t_i18n("Model the Video Studio composes AI music tracks with, unless the assistant is asked for another.")}
              </p>
              <ModelListRefreshing active={refreshing} />
              <ModelSelect
                id={`${fieldId}-music-model`}
                aria-label={t_i18n("Music Generation Model")}
                aria-describedby={`${fieldId}-music-model-hint`}
                models={options}
                providers={providers}
                pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                value={draft || "__none__"}
                onValueChange={(v) => setDraft(v === "__none__" ? "" : v)}
              />
              <SettingsSaveBar
                dirty={draft !== modelChoiceValue(gaConfig?.music_model, gaConfig?.music_provider_id)}
                saving={updateGAConfig.isPending}
                onSave={save}
                onCancel={() => setEditing(false)}
              />
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-start gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-4 shadow-inner backdrop-blur-md">
          <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
            <Info className="h-3 w-3 text-default-secondary" />
          </div>
          <p className="text-[13px] leading-relaxed text-default-secondary">
            {t_i18n(
              "No music-capable provider configured. AI music needs ElevenLabs or Gemini; videos can still use the built-in music library.",
            )}
            {canAddProviders && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
          </p>
        </div>
      )}
    </div>
  );
}
