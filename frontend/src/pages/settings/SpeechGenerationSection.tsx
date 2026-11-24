// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > AI models > Speech generation: the provider, model and
 * voice the chat's read-aloud and the Video Studio voice-over speak with,
 * beside Image and Video generation and built the same way. The models are
 * discovered from the configured providers (`/settings/available-speech-models`):
 * OpenAI and Azure OpenAI, Gemini, ElevenLabs, and the speech models an
 * OpenAI-compatible gateway serves (LiteLLM in front of Amazon Polly, Azure
 * Speech, Vertex AI Chirp...). The voices are the chosen model's
 * (`/settings/speech-voices`): an ElevenLabs account's cloned voices come first.
 * "Auto" leaves the choice to the platform: a voice generator first, else the
 * first provider with a built-in speech model, else the AI Gateway's default;
 * the card names the model it lands on. Opening the picker asks the
 * providers again for their models and voices.
 */
import { useEffect, useId, useState } from "react";
import { SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import { useQueryClient } from "@tanstack/react-query";
import { AudioLines, Check, Info, Pencil } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Input } from "@/components/ui/input";
import { ModelListRefreshing, ModelSelect } from "@/components/ui/model-select";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import {
  useAvailableSpeechModels,
  useMediaModelsInUse,
  useSpeechVoices,
  useUpdateGeneralAssistantConfig,
} from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { sortWithinGroups } from "@/lib/select-options";
import { modelChoiceValue, parseModelChoice, providerDisplayName } from "@/lib/utils";
import type { AvailableModel, GeneralAssistantConfig, LLMProvider, SpeechVoice } from "@/types";

const DEFAULT_VOICE = "__default__";

function voiceOption(voice: SpeechVoice): SelectOption {
  return {
    value: voice.id,
    label: voice.name,
    description: voice.description || undefined,
    group: voice.category ? voice.category.charAt(0).toUpperCase() + voice.category.slice(1) : undefined,
    keywords: [voice.id],
  };
}

export function SpeechGenerationSection({
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
  /** Set by `DEFAULT_SPEECH_PROVIDER`: the environment re-applies model and voice on every restart. */
  envLocked: boolean;
}) {
  const { t_i18n, locale } = useFormatter();
  const fieldId = useId();
  const queryClient = useQueryClient();
  const { data: speechModels, isFetching: refreshing, refetch: refreshModels } = useAvailableSpeechModels();
  const updateGAConfig = useUpdateGeneralAssistantConfig();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [voiceDraft, setVoiceDraft] = useState("");
  // The lock arrives with the config, after a Configure click may have opened the form.
  useEffect(() => {
    if (envLocked) setEditing(false);
  }, [envLocked]);
  const options: AvailableModel[] = speechModels ?? [];
  const current = gaConfig?.speech_model || null;
  // With none saved, the model speech really runs on (a built-in or AI Gateway default).
  const { data: inUse } = useMediaModelsInUse(!!gaConfig && !current);
  const automatic = !current ? inUse?.speech ?? null : null;
  const draftChoice = parseModelChoice(draft || "");
  const draftMatch = draftChoice.model
    ? options.find((m) => m.model === draftChoice.model && (m.provider_id ?? null) === draftChoice.providerId)
      ?? options.find((m) => m.model === draftChoice.model)
    : undefined;
  const voicesQuery = useSpeechVoices(
    draftChoice.model ? draftChoice.providerId ?? draftMatch?.provider_id ?? null : null,
    draftChoice.model || null,
    editing,
  );
  const voices = voicesQuery.data?.voices ?? [];
  const configuredProvider = providerDisplayName(
    gaConfig?.speech_provider ?? options.find((m) => m.model === current)?.provider ?? "",
    gaConfig?.speech_provider_id ?? null,
    providers,
  );

  const startEditing = () => {
    setDraft(modelChoiceValue(gaConfig?.speech_model, gaConfig?.speech_provider_id));
    setVoiceDraft(gaConfig?.speech_voice ?? "");
    setEditing(true);
    void refreshModels();
    void queryClient.invalidateQueries({ queryKey: ["speech-voices"] });
  };

  const save = () => {
    const { model, providerId } = draftChoice;
    updateGAConfig.mutate(
      {
        // "Auto" clears the choice: the platform then takes a voice generator, else the first built-in speech model.
        speech_provider: model ? draftMatch?.provider ?? "" : "",
        speech_provider_id: model ? providerId ?? draftMatch?.provider_id ?? null : null,
        speech_model: model || "",
        speech_voice: model ? voiceDraft.trim() : "",
      },
      { onSuccess: () => setEditing(false) },
    );
  };

  const currentVoice = gaConfig?.speech_voice || null;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <SettingsGroupTitle icon={AudioLines}>{t_i18n("Speech Generation")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n(
              "Used to read answers aloud in the chat and to narrate Video Studio videos: OpenAI, Azure OpenAI, Gemini, ElevenLabs (with your cloned voices) and the speech models an OpenAI-compatible gateway serves (LiteLLM, custom endpoints).",
            )}
          </SettingsGroupDescription>
        </div>
        <div className="ml-4 flex shrink-0 gap-2">
          {envLocked ? (
            <EnvLockBadge />
          ) : isAdmin && options.length > 0 ? (
            !editing ? (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing}>
                <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
              </Button>
            ) : null
          ) : null}
        </div>
      </div>
      {options.length > 0 || current || automatic ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                <AudioLines className="h-4 w-4 text-accent" />
              </div>
              <div>
                <p className="content-base font-medium">
                  {current ?? automatic?.model ?? t_i18n("Auto (provider default)")}
                </p>
                <span className="content-compact text-default-secondary">
                  {current
                    ? currentVoice
                      ? t_i18n("Speech Provider: {provider}, voice {voice}", { provider: configuredProvider, voice: currentVoice })
                      : t_i18n("Speech Provider: {provider}", { provider: configuredProvider })
                    : automatic
                      ? t_i18n("Speech Provider: {provider}", {
                          provider: providerDisplayName(automatic.provider, automatic.provider_id ?? null, providers),
                        })
                      : t_i18n("A voice generator such as ElevenLabs speaks first, else the first provider with a built-in speech model.")}
                </span>
              </div>
            </div>
            {current ? (
              <Badge variant="success" className="content-caption">
                <Check className="mr-1 h-3 w-3" />
                {t_i18n("Active")}
              </Badge>
            ) : automatic ? (
              <Badge variant="secondary" className="content-caption">
                {t_i18n("Automatic")}
              </Badge>
            ) : null}
          </div>
          {editing && (
            <div className="space-y-3">
              <div className="space-y-2">
                <label htmlFor={`${fieldId}-speech-model`} className="content-base font-medium text-default-primary">
                  {t_i18n("Speech Model")}
                </label>
                <p id={`${fieldId}-speech-model-hint`} className="content-compact text-default-secondary">
                  {t_i18n("Model the read-aloud and the voice-over speak with, unless a video names another.")}
                </p>
                <ModelListRefreshing active={refreshing} />
                <ModelSelect
                  id={`${fieldId}-speech-model`}
                  aria-label={t_i18n("Speech Model")}
                  aria-describedby={`${fieldId}-speech-model-hint`}
                  models={options}
                  providers={providers}
                  pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                  value={draft || "__none__"}
                  onValueChange={(v) => {
                    setDraft(v === "__none__" ? "" : v);
                    setVoiceDraft("");
                  }}
                />
              </div>
              {draftChoice.model && (
                <div className="space-y-2">
                  <label htmlFor={`${fieldId}-speech-voice`} className="content-base font-medium text-default-primary">
                    {t_i18n("Voice")}
                  </label>
                  <p id={`${fieldId}-speech-voice-hint`} className="content-compact text-default-secondary">
                    {voicesQuery.isLoading
                      ? t_i18n("Reading the voices of this model...")
                      : voices.length > 0
                        ? t_i18n("The voice everything is spoken with, unless a video names another.")
                        : voicesQuery.data?.message ?? t_i18n("This model lists no voice: type the voice its provider expects.")}
                  </p>
                  {voices.length > 0 ? (
                    <SearchableSelect
                      id={`${fieldId}-speech-voice`}
                      aria-label={t_i18n("Voice")}
                      aria-describedby={`${fieldId}-speech-voice-hint`}
                      options={sortWithinGroups(voices.map(voiceOption), (v) => v.group, (v) => v.label, locale)}
                      sort={false}
                      pinned={[
                        { value: DEFAULT_VOICE, label: t_i18n("Default ({voice})", { voice: voices[0].name }) },
                        ...(voiceDraft && !voices.some((v) => v.id === voiceDraft) ? [{ value: voiceDraft, label: voiceDraft }] : []),
                      ]}
                      pinnedGroup={t_i18n("Defaults")}
                      value={voiceDraft || DEFAULT_VOICE}
                      onValueChange={(v) => setVoiceDraft(v === DEFAULT_VOICE ? "" : v)}
                    />
                  ) : (
                    <Input aria-label={t_i18n("Voice")}
                      id={`${fieldId}-speech-voice`}
                      aria-describedby={`${fieldId}-speech-voice-hint`}
                      value={voiceDraft}
                      maxLength={200}
                      disabled={voicesQuery.isLoading}
                      onChange={(e) => setVoiceDraft(e.target.value)}
                    />
                  )}
                </div>
              )}
              <SettingsSaveBar
                dirty={
                  draft !== modelChoiceValue(gaConfig?.speech_model, gaConfig?.speech_provider_id)
                  || voiceDraft !== (gaConfig?.speech_voice ?? "")
                }
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
              "No speech-capable provider configured. Speech generation needs OpenAI, Azure OpenAI, Gemini or ElevenLabs, or an OpenAI-compatible gateway serving speech models.",
            )}
            {canAddProviders && " " + t_i18n('Click "Add Provider" in this section to configure one.')}
          </p>
        </div>
      )}
    </div>
  );
}
