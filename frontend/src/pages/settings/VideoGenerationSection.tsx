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
 * Settings > AI models > Video generation: the provider and model AI
 * clips are made with, beside Image generation and built the same way. The
 * models are discovered from the configured providers
 * (`/settings/available-video-models`): Sora on OpenAI and Azure OpenAI, Gemini
 * Omni on Gemini, and the video models an OpenAI-compatible gateway serves (LiteLLM in
 * front of Veo on Vertex or Runway, a custom endpoint, the Filigran gateway).
 * "Auto" leaves the choice to the platform: the first provider with a
 * built-in video model. A chosen model its provider retired is shown
 * unavailable with the reason, and can always be changed. Opening the picker
 * asks the providers again.
 */
import { useEffect, useId, useState } from "react";
import { SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Clapperboard, Info, Pencil } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { ModelListRefreshing, ModelSelect } from "@/components/ui/model-select";
import { useAvailableVideoModels, useUpdateGeneralAssistantConfig, useVideoModelStatus } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { modelChoiceValue, parseModelChoice, providerDisplayName } from "@/lib/utils";
import type { AvailableModel, GeneralAssistantConfig, LLMProvider } from "@/types";

export function VideoGenerationSection({
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
  /** Set by `DEFAULT_VIDEO_PROVIDER`: the environment re-applies it on every restart. */
  envLocked: boolean;
}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const queryClient = useQueryClient();
  const { data: videoModels, isFetching: refreshing, refetch: refreshModels } = useAvailableVideoModels();
  const updateGAConfig = useUpdateGeneralAssistantConfig();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  // The lock arrives with the config, after a Configure click may have opened the form.
  useEffect(() => {
    if (envLocked) setEditing(false);
  }, [envLocked]);
  const options: AvailableModel[] = videoModels ?? [];
  const current = gaConfig?.video_model || null;
  const { data: status } = useVideoModelStatus(!!current);
  const unavailable = current ? (status?.unavailable ?? null) : null;
  const configuredProvider = providerDisplayName(
    gaConfig?.video_provider ?? options.find((m) => m.model === current)?.provider ?? "",
    gaConfig?.video_provider_id ?? null,
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
        // "Auto" clears the choice: the platform then takes the first provider with a built-in video model.
        video_provider: model ? matched?.provider ?? "" : "",
        video_provider_id: model ? providerId ?? matched?.provider_id ?? null : null,
        video_model: model || "",
      },
      {
        onSuccess: () => {
          setEditing(false);
          void queryClient.invalidateQueries({ queryKey: ["video-model-status"] });
        },
      },
    );
  };

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <SettingsGroupTitle icon={Clapperboard}>{t_i18n("Video Generation")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n(
              "Used by the Video Studio to generate AI clips from text prompts: Sora (OpenAI, Azure OpenAI), Gemini Omni Flash (Gemini) and the video models an OpenAI-compatible gateway serves (LiteLLM, custom endpoints).",
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
                  setDraft(modelChoiceValue(gaConfig?.video_model, gaConfig?.video_provider_id));
                  setEditing(true);
                  void refreshModels();
                  void queryClient.invalidateQueries({ queryKey: ["video-model-status"] });
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
                <Clapperboard className="h-4 w-4 text-accent" />
              </div>
              <div>
                <p className="content-base font-medium">{current ?? t_i18n("Auto (provider default)")}</p>
                <span className="content-compact text-default-secondary">
                  {current
                    ? t_i18n("Video Provider: {provider}", { provider: configuredProvider })
                    : fallbackProvider
                      ? t_i18n("Clips use the first provider with a built-in video model, such as {provider}.", {
                          provider: fallbackProvider,
                        })
                      : t_i18n('Click "Configure" to pick a default video model')}
                </span>
              </div>
            </div>
            {current &&
              (unavailable ? (
                <Badge variant="warning" className="content-caption">
                  <AlertTriangle className="mr-1 h-3 w-3" />
                  {t_i18n("Unavailable")}
                </Badge>
              ) : (
                <Badge variant="success" className="content-caption">
                  <Check className="mr-1 h-3 w-3" />
                  {t_i18n("Active")}
                </Badge>
              ))}
          </div>
          {unavailable && (
            <div className="flex items-start gap-2 rounded-sm border border-alert-warning bg-alert-warning p-3" data-testid="settings.video-model-unavailable">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <p className="content-compact">
                {t_i18n("{reason}: no AI clip can be made with it.", { reason: unavailable })}{" "}
                {envLocked
                  ? t_i18n("Set by the environment: change DEFAULT_VIDEO_MODEL (or DEFAULT_VIDEO_PROVIDER) to a live video model, then restart.")
                  : options.length > 0
                  ? t_i18n('Click "Configure" to pick a live video model.')
                  : t_i18n(
                      "No configured provider has a live video model: add one whose video models are live - Gemini (Gemini Omni Flash), or a gateway serving Veo or Runway - then pick it here.",
                    )}
              </p>
            </div>
          )}
          {editing && (
            <div className="space-y-2">
              <label htmlFor={`${fieldId}-video-model`} className="content-base font-medium text-default-primary">
                {t_i18n("Video Generation Model")}
              </label>
              <p id={`${fieldId}-video-model-hint`} className="content-compact text-default-secondary">
                {t_i18n("Model the Video Studio generates AI clips with, unless the assistant is asked for another.")}
              </p>
              <ModelListRefreshing active={refreshing} />
              <ModelSelect
                id={`${fieldId}-video-model`}
                aria-label={t_i18n("Video Generation Model")}
                aria-describedby={`${fieldId}-video-model-hint`}
                models={options}
                providers={providers}
                pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                value={draft || "__none__"}
                onValueChange={(v) => setDraft(v === "__none__" ? "" : v)}
              />
              <SettingsSaveBar
                dirty={draft !== modelChoiceValue(gaConfig?.video_model, gaConfig?.video_provider_id)}
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
              "No video-capable provider configured. Video generation needs OpenAI or Gemini, an Azure OpenAI Sora deployment, or an OpenAI-compatible gateway serving video models.",
            )}
            {canAddProviders && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
          </p>
        </div>
      )}
    </div>
  );
}
