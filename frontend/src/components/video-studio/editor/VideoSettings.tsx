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
 * The whole video's settings: format, template or free style, theme,
 * voice-over (its pronunciations included), captions, music and the clips'
 * own sound (`MusicSettings`) and transitions - the spec's own fields, each
 * left empty to let the template (or the default) decide, each locked when
 * the template keeps it.
 */
import { useRef, useState } from "react";
import { Activity, Captions, Film, LayoutTemplate, Mic, Music, Palette, Timer, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { usePronunciationLexicon, useUploadVideoFile, useVideoTemplates } from "@/hooks/api/useVideoStudioApi";
import { useAvailableSpeechModels, useSpeechVoices } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import type { PronunciationEntry, VideoTemplateSummary } from "@/types";

import { PronunciationEntriesEditor, PronunciationEntriesList } from "../PronunciationEntries";
import { usePronunciationPlayer } from "../usePronunciationPlayer";

import {
  BACKGROUND_LABELS,
  BEAT_SYNC_LABELS,
  CAPTION_POSITION_LABELS,
  CAPTION_STYLE_LABELS,
  CLIP_SOUND_LABELS,
  MOTION_PERSONALITY_LABELS,
  THEME_MODE_LABELS,
  TITLE_SPLIT_LABELS,
  TRANSITION_LABELS,
  TRANSITION_PACE_LABELS,
  TRANSITION_SECONDS,
  WATERMARK_LABELS,
  choicesOf,
  isLockedBy,
} from "../video-format";
import { ColorField, Field, NumberField, Section, SelectField, SwitchField, TextAreaField, TextField } from "./fields";
import { MusicSettings } from "./MusicSettings";
import type { EditorSpec } from "./video-editor-model";
import { Spinner } from "@filigran/design-system";

type Patch = Record<string, unknown>;
const FREESTYLE = "__freestyle__";
const PLATFORM = "__platform__";
const ASPECTS = ["16:9", "9:16", "1:1", "4:5"] as const;
const FRAME_RATES = ["24", "25", "30", "50", "60"] as const;

const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
const num = (value: unknown): number | undefined => (typeof value === "number" ? value : undefined);
const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);

/** The most entries a video holds (`spec.Voiceover.pronunciations`). */
const VIDEO_ENTRIES_MAX = 60;
/** The platform entries listed before "Show all". */
const PLATFORM_SHOWN = 8;

/** The same written form, as the server decides an override (`lexicon.same_form`). */
function sameForm(a: PronunciationEntry, b: PronunciationEntry): boolean {
  const key = (e: PronunciationEntry) => e.text.trim().split(/\s+/).join(" ");
  if (key(a) === key(b)) return true;
  return (a.match === "case_insensitive" || b.match === "case_insensitive") && key(a).toLowerCase() === key(b).toLowerCase();
}

/**
 * The video's pronunciation lexicon: its own entries, edited,
 * then the platform's it does not override, each with a play button that
 * speaks the term as this video's voice-over says it.
 */
function PronunciationsField({
  value,
  voiceover,
  onChange,
  disabled,
}: {
  value: PronunciationEntry[];
  voiceover: Record<string, unknown>;
  onChange: (rows: PronunciationEntry[]) => void;
  disabled: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: platform } = usePronunciationLexicon();
  const player = usePronunciationPlayer(voiceover);
  const [all, setAll] = useState(false);
  const inherited = (platform?.entries ?? []).filter((entry) => !value.some((own) => sameForm(own, entry)));
  const shown = all ? inherited : inherited.slice(0, PLATFORM_SHOWN);
  return (
    <Field label={t_i18n("Pronunciations")} hint={t_i18n("Product names and acronyms the voice would misread. The captions keep the written words.")}>
      <PronunciationEntriesEditor
        value={value}
        onChange={onChange}
        disabled={disabled}
        max={VIDEO_ENTRIES_MAX}
        player={player}
        testId="video.editor.pronunciations"
      />
      {inherited.length > 0 && (
        <div className="space-y-1 pt-2" data-testid="video.editor.platform-pronunciations">
          <p className="content-caption text-default-secondary">
            {t_i18n("From the platform lexicon (version {version}): an entry of this video with the same written form replaces it.", {
              version: platform?.version ?? 0,
            })}
          </p>
          <PronunciationEntriesList
            entries={shown}
            player={player}
            action={(entry) => (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 px-1.5 content-caption"
                disabled={disabled || value.length >= VIDEO_ENTRIES_MAX}
                onClick={() => onChange([...value, entry])}
              >
                {t_i18n("Override")}
              </Button>
            )}
          />
          {inherited.length > PLATFORM_SHOWN && (
            <Button variant="ghost" size="sm" className="h-6 px-1 content-caption" onClick={() => setAll(!all)}>
              {all ? t_i18n("Show fewer") : t_i18n("Show all {count}", { count: inherited.length })}
            </Button>
          )}
        </div>
      )}
    </Field>
  );
}

export function VideoSettings({
  draft,
  template,
  canEdit,
  plannedSeconds,
  onChange,
}: {
  draft: EditorSpec;
  /** The template the video wears (its locks), `null` in free style. */
  template: VideoTemplateSummary | null;
  canEdit: boolean;
  /** The video's planned length (the preview's): an AI track is composed for it. */
  plannedSeconds?: number;
  onChange: (patch: Patch, coalesce?: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: templates } = useVideoTemplates();
  const { data: speechModels } = useAvailableSpeechModels();
  const upload = useUploadVideoFile();
  const logoInput = useRef<HTMLInputElement>(null);
  const off = !canEdit;
  const locked = template?.locked;
  const lockedPath = (path: string) => isLockedBy(locked, path);
  // The corner the template fills an unset watermark with: "None" is then a choice of its own.
  const templateCorner = template?.watermark ?? null;
  const format = obj(draft.format);
  const theme = obj(draft.theme);
  const voice = obj(draft.voiceover);
  const captions = obj(draft.captions);
  const music = draft.music ? obj(draft.music) : null;
  const transition = obj(draft.default_transition);
  const motion = obj(theme.motion);
  // An unset motion field takes the template's (`templates.apply_template` lays the spec over its defaults).
  const templateMotion = template?.motion ?? {};
  const blurred = (str(motion.motion_blur) ?? templateMotion.motion_blur) === "on";
  const set = (group: string, key: string) => (value: unknown) =>
    onChange({ [group]: { [key]: value ?? null } }, `video.${group}.${key}`);
  const setMotion = (key: string) => (value: unknown) =>
    onChange({ theme: { motion: { [key]: value ?? null } } }, `video.theme.motion.${key}`);
  const speaking = voice.enabled !== false;
  // A model named without its provider is still the video's own choice, never the platform's.
  const voiceModelKey = voice.provider_id || voice.model ? `${str(voice.provider_id) ?? ""}::${str(voice.model) ?? ""}` : PLATFORM;
  const voices = useSpeechVoices(str(voice.provider_id) ?? null, str(voice.model) ?? null, speaking);
  const platformVoices = useSpeechVoices(null, null, speaking);
  const platformModel = platformVoices.data?.model;
  const speechModelOptions = (speechModels ?? []).filter((m) => m.provider_id).map((m) => ({ value: `${m.provider_id}::${m.model}`, label: `${m.model} - ${m.provider}` }));
  if (voiceModelKey !== PLATFORM && !speechModelOptions.some((o) => o.value === voiceModelKey)) {
    const provider = speechModels?.find((m) => m.provider_id === voice.provider_id)?.provider;
    const model = str(voice.model) ?? t_i18n("The provider's default model");
    speechModelOptions.unshift({ value: voiceModelKey, label: provider ? `${model} - ${provider}` : model });
  }
  const ratios = template?.aspect_ratios?.length ? template.aspect_ratios : [...ASPECTS];

  const uploadInto = async (file: File | undefined, apply: (fileId: string) => void) => {
    if (!file) return;
    try {
      const stored = await upload.mutateAsync(file);
      apply(stored.file_id);
    } catch (err) {
      toast({ title: t_i18n("Upload failed"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  return (
    <div data-testid="video.editor.video-settings">
      <Section title={t_i18n("Video")} icon={Film}>
        <TextField label={t_i18n("Title (heading)")} value={draft.title} maxLength={200} onChange={(title) => onChange({ title: title ?? "" }, "video.title")} disabled={off} />
        <div className="grid grid-cols-2 gap-2">
          <SelectField
            label={t_i18n("Format")}
            value={(str(format.aspect_ratio) as (typeof ASPECTS)[number] | undefined) ?? "16:9"}
            options={ratios.map((value) => ({ value: value as (typeof ASPECTS)[number], label: value }))}
            onChange={(v) => onChange({ format: { aspect_ratio: v ?? null } })}
            disabled={off}
          />
          <SelectField
            label={t_i18n("Frame rate")}
            value={format.fps ? (String(format.fps) as (typeof FRAME_RATES)[number]) : undefined}
            unsetLabel={t_i18n("The quality's")}
            options={FRAME_RATES.map((value) => ({ value, label: `${value} fps` }))}
            onChange={(v) => onChange({ format: { fps: v ? Number(v) : null } })}
            disabled={off}
          />
        </div>
        <SwitchField label={t_i18n("Fade in from black")} checked={draft.fade_in !== false} onChange={(v) => onChange({ fade_in: v ? null : false })} disabled={off} />
        <SwitchField label={t_i18n("Fade out to black")} checked={draft.fade_out !== false} onChange={(v) => onChange({ fade_out: v ? null : false })} disabled={off} />
      </Section>

      <Section title={t_i18n("Template")} icon={LayoutTemplate}>
        <SelectField
          label={t_i18n("Look")}
          value={draft.freestyle ? FREESTYLE : (str(draft.template_id) ?? template?.id ?? undefined)}
          unsetLabel={draft.freestyle ? undefined : t_i18n("The platform default")}
          options={[
            ...(templates ?? []).map((row) => ({ value: row.id, label: row.is_default ? `${row.name} (${t_i18n("Default")})` : row.name })),
            { value: FREESTYLE, label: t_i18n("Free style") },
          ]}
          onChange={(value) =>
            onChange(value === FREESTYLE ? { freestyle: true, template_id: null } : { freestyle: null, template_id: value ?? null })
          }
          hint={t_i18n("A template dresses every video the same way: its palette, its cards and the choices it locks.")}
          disabled={off}
        />
        {template?.guidance && <p className="rounded-sm bg-elevation-hover p-2 text-[11px] text-default-secondary">{template.guidance}</p>}
      </Section>

      <Section title={t_i18n("Theme")} icon={Palette} defaultOpen={false}>
        <div className="grid grid-cols-2 gap-2">
          <SelectField label={t_i18n("Colour scheme")} value={str(theme.mode) as keyof typeof THEME_MODE_LABELS | undefined} unsetLabel={t_i18n("Dark")} options={choicesOf(THEME_MODE_LABELS, t_i18n).filter((o) => o.value !== "dark")} onChange={set("theme", "mode")} locked={lockedPath("theme.mode")} disabled={off} />
          <SelectField label={t_i18n("Backdrop")} value={str(theme.background) as keyof typeof BACKGROUND_LABELS | undefined} unsetLabel={t_i18n("Template's")} options={choicesOf(BACKGROUND_LABELS, t_i18n)} onChange={set("theme", "background")} locked={lockedPath("theme.background")} disabled={off} />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <ColorField label={t_i18n("Accent")} value={str(theme.accent)} placeholder={t_i18n("Template's")} onChange={set("theme", "accent")} locked={lockedPath("theme.palette")} disabled={off} />
          <ColorField label={t_i18n("Second accent")} value={str(theme.accent_2)} placeholder={t_i18n("Template's")} onChange={set("theme", "accent_2")} locked={lockedPath("theme.palette")} disabled={off} />
          <ColorField label={t_i18n("Background")} value={str(theme.background_color)} placeholder={t_i18n("Template's")} onChange={set("theme", "background_color")} locked={lockedPath("theme.palette")} disabled={off} />
          <ColorField label={t_i18n("Text")} value={str(theme.text_color)} placeholder={t_i18n("Template's")} onChange={set("theme", "text_color")} locked={lockedPath("theme.palette")} disabled={off} />
        </div>
        <SelectField
          label={t_i18n("Watermark")}
          value={str(theme.watermark) as keyof typeof WATERMARK_LABELS | undefined}
          unsetLabel={templateCorner ? t_i18n("Template's ({corner})", { corner: t_i18n(WATERMARK_LABELS[templateCorner]) }) : t_i18n("None")}
          options={choicesOf(WATERMARK_LABELS, t_i18n).filter((o) => templateCorner || o.value !== "none")}
          onChange={set("theme", "watermark")}
          hint={templateCorner && theme.watermark === "none" ? t_i18n("Its template draws its mark in a corner: this video draws none.") : undefined}
          locked={lockedPath("theme.watermark")}
          disabled={off}
        />
        <Field label={t_i18n("Logo")} locked={lockedPath("theme.palette")}>
          <div className="flex items-center gap-2">
            <input ref={logoInput} type="file" accept="image/*" className="hidden" onChange={(e) => void uploadInto(e.target.files?.[0], (id) => set("theme", "logo_file_id")(id))} />
            <Button variant="outline" size="sm" className="h-7 gap-1.5 content-compact" disabled={off || upload.isPending || lockedPath("theme.palette")} onClick={() => logoInput.current?.click()} aria-busy={upload.isPending || undefined}>
              {upload.isPending && <Spinner size="sm" tone="inherit" />}{!upload.isPending && <Upload className="h-3 w-3" />}
              {theme.logo_file_id ? t_i18n("Replace") : t_i18n("Upload")}
            </Button>
            {!!theme.logo_file_id && (
              <Button variant="ghost" size="sm" className="h-7 content-compact" disabled={off} onClick={() => set("theme", "logo_file_id")(null)}>
                {t_i18n("Use the template's")}
              </Button>
            )}
          </div>
        </Field>
      </Section>

      <Section title={t_i18n("Motion")} icon={Activity} defaultOpen={false}>
        <SelectField
          label={t_i18n("Personality")}
          value={str(motion.personality) as keyof typeof MOTION_PERSONALITY_LABELS | undefined}
          unsetLabel={templateMotion.personality ? t_i18n("Template's ({motion})", { motion: t_i18n(MOTION_PERSONALITY_LABELS[templateMotion.personality]) }) : t_i18n(MOTION_PERSONALITY_LABELS.precise)}
          options={choicesOf(MOTION_PERSONALITY_LABELS, t_i18n)}
          onChange={setMotion("personality")}
          hint={t_i18n("How every card, statement, overlay and motion scene moves: its timing, its easing, how its titles come in.")}
          locked={lockedPath("theme.motion.personality") || lockedPath("theme.motion")}
          disabled={off}
        />
        <div className="grid grid-cols-2 gap-2">
          <NumberField label={t_i18n("Speed")} unit="x" min={0.5} max={2} step={0.05} value={num(motion.speed)} fallback={templateMotion.speed ?? 1} onChange={setMotion("speed")} locked={lockedPath("theme.motion")} disabled={off} />
          <SelectField
            label={t_i18n("Titles come in by")}
            value={str(motion.title) as keyof typeof TITLE_SPLIT_LABELS | undefined}
            unsetLabel={templateMotion.title ? t_i18n("Template's ({motion})", { motion: t_i18n(TITLE_SPLIT_LABELS[templateMotion.title]) }) : t_i18n("The personality's")}
            options={choicesOf(TITLE_SPLIT_LABELS, t_i18n)}
            onChange={setMotion("title")}
            locked={lockedPath("theme.motion")}
            disabled={off}
          />
        </div>
        <SelectField
          label={t_i18n("On the beat")}
          value={str(motion.beat_sync) as keyof typeof BEAT_SYNC_LABELS | undefined}
          unsetLabel={templateMotion.beat_sync && templateMotion.beat_sync !== "off" ? t_i18n("Template's ({motion})", { motion: t_i18n(BEAT_SYNC_LABELS[templateMotion.beat_sync]) }) : t_i18n(BEAT_SYNC_LABELS.off)}
          options={choicesOf(BEAT_SYNC_LABELS, t_i18n).filter((o) => o.value !== "off" || (templateMotion.beat_sync ?? "off") !== "off")}
          onChange={setMotion("beat_sync")}
          hint={t_i18n("Scenes play a little longer so each cut lands on a beat of the music; with builds, lines and elements come in on beats too.")}
          locked={lockedPath("theme.motion")}
          disabled={off || !music}
        />
        <SwitchField
          label={t_i18n("Motion blur")}
          checked={blurred}
          onChange={(v) => {
            const value = v ? "on" : "off";
            setMotion("motion_blur")(value === (templateMotion.motion_blur ?? "off") ? null : value);
          }}
          hint={t_i18n("What moves blurs as on film. Drawn at the high and ultra qualities only, it takes about three times as long to render.")}
          locked={lockedPath("theme.motion")}
          disabled={off}
        />
      </Section>

      <Section title={t_i18n("Voice-over")} icon={Mic} defaultOpen={false}>
        <SwitchField label={t_i18n("Speak the narration")} checked={speaking} onChange={(v) => set("voiceover", "enabled")(v ? null : false)} hint={t_i18n("Off: the narration shows as captions only.")} disabled={off} />
        {speaking && (
          <>
            <SelectField
              label={t_i18n("Speech model")}
              value={voiceModelKey === PLATFORM ? undefined : voiceModelKey}
              unsetLabel={platformModel ? t_i18n("The platform's speech model ({model})", { model: platformModel }) : t_i18n("The platform's speech model")}
              options={speechModelOptions}
              onChange={(value) => {
                const [providerId, model] = value ? value.split("::") : [null, null];
                onChange({ voiceover: { provider_id: providerId || null, model: model || null, voice: null } });
              }}
              hint={voiceModelKey === PLATFORM ? undefined : t_i18n("This video keeps its own speech model when the platform's changes.")}
              disabled={off}
            />
            <SelectField
              label={t_i18n("Voice")}
              value={str(voice.voice)}
              unsetLabel={t_i18n("The model's default voice")}
              options={(voices.data?.voices ?? []).map((v) => ({ value: v.id, label: v.category ? `${v.name} (${v.category})` : v.name }))}
              onChange={set("voiceover", "voice")}
              hint={voices.isLoading ? t_i18n("Loading the voices...") : (voices.data?.message ?? undefined)}
              locked={lockedPath("voiceover.voice")}
              disabled={off}
            />
            <TextAreaField label={t_i18n("Delivery")} value={str(voice.style)} rows={2} maxLength={500} placeholder={t_i18n("Warm, confident, never rushed...")} onChange={set("voiceover", "style")} hint={t_i18n("For the models that take a delivery instruction.")} disabled={off || lockedPath("voiceover.style")} />
            <NumberField label={t_i18n("Pace")} unit="x" min={0.7} max={1.3} step={0.05} value={num(voice.speed)} fallback={1} onChange={set("voiceover", "speed")} disabled={off} />
            <NumberField label={t_i18n("Volume")} min={0} max={2} step={0.05} value={num(voice.volume)} fallback={1} onChange={set("voiceover", "volume")} disabled={off} />
            <PronunciationsField
              value={Array.isArray(voice.pronunciations) ? (voice.pronunciations as PronunciationEntry[]) : []}
              voiceover={voice}
              onChange={(rows) => onChange({ voiceover: { pronunciations: rows.length ? rows : null } }, "video.voiceover.pronunciations")}
              disabled={off}
            />
          </>
        )}
      </Section>

      <Section title={t_i18n("Captions")} icon={Captions} defaultOpen={false}>
        <SwitchField label={t_i18n("Captions")} checked={captions.enabled !== false} onChange={(v) => set("captions", "enabled")(v ? null : false)} locked={lockedPath("captions")} disabled={off} />
        {captions.enabled !== false && (
          <>
            <SwitchField label={t_i18n("Burn them into the video")} checked={captions.burn_in !== false} onChange={(v) => set("captions", "burn_in")(v ? null : false)} hint={t_i18n("A WebVTT file is produced either way.")} locked={lockedPath("captions")} disabled={off} />
            <div className="grid grid-cols-2 gap-2">
              <SelectField label={t_i18n("Style")} value={str(captions.style) as keyof typeof CAPTION_STYLE_LABELS | undefined} unsetLabel={t_i18n("Pill")} options={choicesOf(CAPTION_STYLE_LABELS, t_i18n).filter((o) => o.value !== "pill")} onChange={set("captions", "style")} locked={lockedPath("captions.style")} disabled={off} />
              <SelectField label={t_i18n("Position")} value={str(captions.position) as keyof typeof CAPTION_POSITION_LABELS | undefined} unsetLabel={t_i18n("Bottom")} options={choicesOf(CAPTION_POSITION_LABELS, t_i18n).filter((o) => o.value !== "bottom")} onChange={set("captions", "position")} locked={lockedPath("captions.position")} disabled={off} />
            </div>
          </>
        )}
      </Section>

      <Section title={t_i18n("Music and sound")} icon={Music} defaultOpen={!!music}>
        <MusicSettings music={music} plannedSeconds={plannedSeconds} canEdit={canEdit} onChange={onChange} />
        <SelectField
          label={t_i18n("The clips' own sound")}
          value={str(draft.clip_sound) as keyof typeof CLIP_SOUND_LABELS | undefined}
          unsetLabel={t_i18n("Automatic")}
          options={choicesOf(CLIP_SOUND_LABELS, t_i18n)}
          onChange={(v) => onChange({ clip_sound: v ?? null })}
          hint={t_i18n(
            "Automatic: an AI shot's own sound gives way to the music, an uploaded video keeps its sound. A scene can choose for itself. The voice always leads: the music and the clips' sound are levelled with it and go down under every sentence.",
          )}
          disabled={off}
        />
      </Section>

      <Section title={t_i18n("Transitions")} icon={Timer} defaultOpen={false}>
        <SelectField
          label={t_i18n("Between scenes")}
          value={str(transition.type) as keyof typeof TRANSITION_LABELS | undefined}
          unsetLabel={template ? t_i18n("Template's") : t_i18n("Automatic")}
          options={choicesOf(TRANSITION_LABELS, t_i18n)}
          onChange={set("default_transition", "type")}
          locked={lockedPath("default_transition")}
          hint={t_i18n("Automatic picks each cut from the scenes on either side: the opening card pushes into the video, words dissolve into pictures, the closing card comes after a dip to black.")}
          disabled={off}
        />
        {(!transition.type || transition.type === "auto") && (
          <SelectField
            label={t_i18n("Rhythm")}
            value={str(transition.pace) as keyof typeof TRANSITION_PACE_LABELS | undefined}
            unsetLabel={template ? t_i18n("Template's") : t_i18n(TRANSITION_PACE_LABELS.calm)}
            options={choicesOf(TRANSITION_PACE_LABELS, t_i18n)}
            onChange={set("default_transition", "pace")}
            locked={lockedPath("default_transition")}
            disabled={off}
          />
        )}
        {typeof transition.type === "string" && transition.type !== "cut" && transition.type !== "auto" && (
          <NumberField label={t_i18n("Duration")} unit="s" min={0.1} max={2} step={0.05} value={num(transition.duration)} fallback={TRANSITION_SECONDS[transition.type as keyof typeof TRANSITION_SECONDS] ?? 0.6} onChange={set("default_transition", "duration")} locked={lockedPath("default_transition")} disabled={off} />
        )}
      </Section>
    </div>
  );
}
