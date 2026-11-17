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
 * "New video": one brief, and the editor opens on
 * a video the assistant is already making - of any kind.
 *
 * - The kind of video is optional: a creative one (just for fun, a short
 *   story, a how-to, a greeting) or one for work (product demo, brand film,
 *   event loop, launch, threat explainer, social teaser). It opens the request
 *   with its words, and a kind for work suggests the template made for it
 *   (`templatesForKind`) and its format - never over a look or a format the
 *   person chose (`useNewVideoChoices`).
 * - The look is free style until something asks for a template: no brand, no
 *   added cards. What the dialog shows is what both buttons create.
 * - The request is shown before anything happens - the kind's sentence, then
 *   the brief as typed - and "Create" sends it as the first message of the
 *   studio rail bound to the new video, on the assistant picked here (the
 *   ones carrying the Video Studio tool, `useToolAgents`). The video starts
 *   untitled: its title is the assistant's to write, never the brief.
 * - A blank video, edited by hand, stays one click away.
 */
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Bot,
  Clapperboard,
  FilePlus2,
  Film,
  Gift,
  GraduationCap,
  Lightbulb,
  MonitorPlay,
  Moon,
  Palette,
  PartyPopper,
  Rocket,
  Send,
  ShieldAlert,
  Smartphone,
  Sparkles,
  Sun,
  Tv,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useCreateVideoProject, useMusicLibrary, useVideoTemplates } from "@/hooks/api/useVideoStudioApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoEditorLaunch } from "@/pages/VideoEditor";
import type { VideoTemplateSummary } from "@/types";

import { VideoTemplateRules } from "./VideoTemplateRules";
import {
  ASPECT_RATIO_LABELS,
  exampleBrief,
  isTemplatePurpose,
  NEW_VIDEO_CHOICE_LABELS,
  NEW_VIDEO_CHOICES_UNSET,
  newVideoChoiceLines,
  newVideoChoicesSet,
  newVideoRequest,
  newVideoSpecChoices,
  type NewVideoChoices,
  templatesForKind,
  VIDEO_KIND_EXAMPLES,
  VIDEO_KIND_GROUP_LABELS,
  VIDEO_KIND_GROUPS,
  VIDEO_KIND_LABELS,
  VIDEO_KIND_REQUESTS,
  type VideoKind,
} from "./video-format";
import { VIDEO_TOOL_ID } from "./video-handoff";
import { FREESTYLE, useNewVideoChoices } from "./useNewVideoChoices";
import { Spinner } from "@filigran/design-system";

/** Picker value for the General Assistant as the video's maker. */
const GENERAL = "__general__";
const KIND_ICONS: Record<VideoKind, React.ElementType> = {
  fun: PartyPopper,
  story: BookOpen,
  howto: GraduationCap,
  greeting: Gift,
  product_demo: MonitorPlay,
  brand_film: Film,
  event: Tv,
  launch: Rocket,
  explainer: ShieldAlert,
  social: Smartphone,
};
/** Each format's frame, drawn to its shape. */
const RATIO_SHAPES: Record<string, string> = { "16:9": "h-2.5 w-4", "9:16": "h-4 w-2.5", "1:1": "h-3.5 w-3.5", "4:5": "h-4 w-3" };

function TemplateChoice({
  template,
  selected,
  suggested,
  onSelect,
}: {
  template: VideoTemplateSummary;
  selected: boolean;
  suggested: boolean;
  onSelect: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "w-full rounded-sm border p-3 text-left transition-colors",
        selected ? "border-accent/50 bg-accent/5" : "border-elevation-subtle hover:border-elevation-subtle-soft hover:bg-elevation-hover",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="content-base font-semibold">{template.name}</span>
        {suggested && (
          <Badge variant="cyan" className="content-caption">
            {t_i18n("Suggested")}
          </Badge>
        )}
        <span className="ml-auto flex items-center gap-1 text-default-secondary">
          {template.supported_schemes.includes("dark") && <Moon className="h-3 w-3" aria-label={t_i18n("Dark")} />}
          {template.supported_schemes.includes("light") && <Sun className="h-3 w-3" aria-label={t_i18n("Light")} />}
        </span>
      </div>
      <Swatches template={template} className="mt-2 h-2" />
      {template.description && <p className="mt-2 line-clamp-2 content-compact text-default-secondary">{template.description}</p>}
      <div className="mt-2">
        <VideoTemplateRules rules={template} compact />
      </div>
    </button>
  );
}

function Swatches({ template, className }: { template: VideoTemplateSummary; className?: string }) {
  const colors = template.series.length ? template.series : template.swatches;
  if (!colors.length) return null;
  return (
    <div className={cn("flex overflow-hidden rounded-full", className)} aria-hidden>
      {colors.map((color, index) => (
        // Data-driven colour: the template's own palette.
        <span key={`${color}-${index}`} className="flex-1" style={{ backgroundColor: color }} />
      ))}
    </div>
  );
}

function ChoiceChip({
  selected,
  onClick,
  icon: Icon,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  icon?: React.ElementType;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-sm border px-2.5 py-1.5 content-compact transition-colors",
        selected ? "border-accent/50 bg-accent/10 text-default-primary" : "border-elevation-subtle text-default-secondary hover:bg-elevation-hover",
      )}
    >
      {Icon && <Icon className={cn("h-4 w-4 shrink-0", selected ? "text-accent" : "text-default-secondary")} />}
      {children}
    </button>
  );
}

/**
 * A field under an option: its label above it, the field, then one help line
 * always visible (design standards s.2), which the field names through
 * aria-describedby `${id}-help`. A design-system Input passes its help as
 * `helperText` instead, so the help and the `N/max` counter share the Input's
 * helper row (usage-contracts/Input.md 1.5.1, UX #11540 r3 B1); the seconds
 * field writes its own help or error.
 */
function OptionField({ id, label, help, children }: { id: string; label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label id={`${id}-label`}>{label}</Label>
      {children}
      {help && (
        <p id={`${id}-help`} className="content-compact text-default-secondary">
          {help}
        </p>
      )}
    </div>
  );
}

/** The target length the field accepts, in whole seconds. */
const MIN_SECONDS = 5;
const MAX_SECONDS = 600;

/** The seconds typed, or null when the text is not a whole number from 5 to 600. */
function validSeconds(text: string): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const seconds = Number(text.trim());
  return seconds >= MIN_SECONDS && seconds <= MAX_SECONDS ? seconds : null;
}

/** One optional choice of "More options": its label, then its chips, "suggest" first. */
function OptionRow({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label id={id}>{label}</Label>
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-labelledby={id}>
        {children}
      </div>
    </div>
  );
}

/**
 * "More options": music, narration, captions, length and pace, each left to
 * the assistant until the person sets it (Sam, 13 Nov 2026, #11538: the
 * studio proposes, the person chooses).
 */
function NewVideoOptions({
  open,
  choices,
  onChange,
  disabled,
}: {
  open: boolean;
  choices: NewVideoChoices;
  onChange: (patch: Partial<NewVideoChoices>) => void;
  disabled: boolean;
}) {
  const { t_i18n } = useFormatter();
  const library = useMusicLibrary(open && choices.music === "track");
  const L = NEW_VIDEO_CHOICE_LABELS;
  // The length as typed: only a whole number from 5 to 600 reaches the
  // request; anything else is kept in the field and marked invalid.
  const [secondsText, setSecondsText] = useState(choices.seconds > 0 ? String(choices.seconds) : "");
  const secondsInvalid = secondsText.trim() !== "" && validSeconds(secondsText) === null;
  const chip = <K extends keyof NewVideoChoices>(key: K, value: NewVideoChoices[K], label: string) => (
    <ChoiceChip selected={choices[key] === value} onClick={() => !disabled && onChange({ [key]: value } as Partial<NewVideoChoices>)}>
      {t_i18n(label)}
    </ChoiceChip>
  );
  return (
    <div className="space-y-3 rounded-sm border border-elevation-subtle p-3" data-testid="video.new.options">
      <OptionRow id="vs-new-music" label={t_i18n("Music")}>
        {chip("music", "suggest", L.suggest)}
        {chip("music", "none", L.music_none)}
        {chip("music", "track", L.music_track)}
        {chip("music", "ai", L.music_ai)}
      </OptionRow>
      {choices.music === "track" && (
        <OptionField
          id="vs-new-music-track"
          label={t_i18n("Track")}
          help={t_i18n("A built-in track from the studio library, played under the video.")}
        >
          <SearchableSelect
            aria-labelledby="vs-new-music-track-label"
            aria-describedby="vs-new-music-track-help"
            options={(library.data ?? []).map((track) => ({ value: track.id, label: track.name, description: track.mood }))}
            value={choices.musicTrack || null}
            onValueChange={(value) =>
              onChange({ musicTrack: value ?? "", musicTrackName: (library.data ?? []).find((track) => track.id === value)?.name ?? "" })
            }
            disabled={disabled}
            placeholder={library.isLoading ? t_i18n("Loading tracks...") : t_i18n("Pick a track")}
          />
        </OptionField>
      )}
      {choices.music === "ai" && (
        <OptionField id="vs-new-music-mood" label={t_i18n("Mood")}>
          <Input
            id="vs-new-music-mood"
            aria-labelledby="vs-new-music-mood-label"
            helperText={t_i18n("The assistant composes a track from this line.")}
            value={choices.musicMood}
            maxLength={300}
            disabled={disabled}
            placeholder={t_i18n("The mood in a few words: warm piano, slow build...")}
            onChange={(event) => onChange({ musicMood: event.target.value })}
          />
        </OptionField>
      )}
      <OptionRow id="vs-new-narration" label={t_i18n("Narration")}>
        {chip("narration", "suggest", L.suggest)}
        {chip("narration", "none", L.narration_none)}
        {chip("narration", "on", L.narration_on)}
      </OptionRow>
      {choices.narration === "on" && (
        <div className="grid gap-2 sm:grid-cols-2">
          <OptionField id="vs-new-narration-language" label={t_i18n("Language (optional)")}>
            <Input
              id="vs-new-narration-language"
              aria-labelledby="vs-new-narration-language-label"
              helperText={t_i18n("Empty lets the assistant use the language of the brief.")}
              value={choices.narrationLanguage}
              maxLength={60}
              disabled={disabled}
              placeholder={t_i18n("French, English...")}
              onChange={(event) => onChange({ narrationLanguage: event.target.value })}
            />
          </OptionField>
          <OptionField id="vs-new-narration-voice" label={t_i18n("Voice (optional)")}>
            <Input
              id="vs-new-narration-voice"
              aria-labelledby="vs-new-narration-voice-label"
              helperText={t_i18n("Empty lets the assistant pick a voice that fits the brief.")}
              value={choices.narrationVoice}
              maxLength={120}
              disabled={disabled}
              placeholder={t_i18n("Warm, calm, energetic...")}
              onChange={(event) => onChange({ narrationVoice: event.target.value })}
            />
          </OptionField>
        </div>
      )}
      <OptionRow id="vs-new-captions" label={t_i18n("Captions")}>
        {chip("captions", "suggest", L.suggest)}
        {chip("captions", "off", L.captions_off)}
        {chip("captions", "file", L.captions_file)}
        {chip("captions", "burned", L.captions_burned)}
      </OptionRow>
      <OptionRow id="vs-new-length" label={t_i18n("Length")}>
        <ChoiceChip
          selected={secondsText.trim() === ""}
          onClick={() => {
            if (disabled) return;
            setSecondsText("");
            onChange({ seconds: 0 });
          }}
        >
          {t_i18n(L.suggest)}
        </ChoiceChip>
      </OptionRow>
      <OptionField id="vs-new-seconds" label={t_i18n("Target length in seconds (optional)")}>
        <Input
          type="number"
          min={MIN_SECONDS}
          max={MAX_SECONDS}
          step={1}
          aria-labelledby="vs-new-seconds-label"
          aria-invalid={secondsInvalid || undefined}
          aria-describedby={secondsInvalid ? "vs-new-seconds-error" : "vs-new-seconds-help"}
          className="w-40"
          value={secondsText}
          disabled={disabled}
          onChange={(event) => {
            setSecondsText(event.target.value);
            onChange({ seconds: validSeconds(event.target.value) ?? 0 });
          }}
        />
        {secondsInvalid ? (
          <p id="vs-new-seconds-error" className="content-compact text-feedback-error-primary">
            {t_i18n("Enter a whole number of seconds from 5 to 600.")}
          </p>
        ) : (
          <p id="vs-new-seconds-help" className="content-compact text-default-secondary">
            {t_i18n("From 5 to 600 seconds. Empty leaves the length to the assistant.")}
          </p>
        )}
      </OptionField>
      <OptionRow id="vs-new-pace" label={t_i18n("Pace")}>
        {chip("pace", "suggest", L.suggest)}
        {chip("pace", "calm", L.pace_calm)}
        {chip("pace", "dynamic", L.pace_dynamic)}
      </OptionRow>
    </div>
  );
}

export function NewVideoDialog({
  open,
  onOpenChange,
  templateId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pre-selected template (a template page's "New video with this template"). */
  templateId?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const aiOff = useAiActionDisabled();
  const { data: templates, isLoading } = useVideoTemplates(open);
  const rows = useMemo(() => templates ?? [], [templates]);
  const createProject = useCreateVideoProject();
  const { kind, chooseKind, pick, choosePick, picked, suggested, ratio, setRatio, ratios, brief, setBrief } = useNewVideoChoices(
    open,
    rows,
    templateId,
  );
  const [looksOpen, setLooksOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [choices, setChoices] = useState<NewVideoChoices>(NEW_VIDEO_CHOICES_UNSET);
  const changeChoices = (patch: Partial<NewVideoChoices>) => setChoices((current) => ({ ...current, ...patch }));
  // Who makes it: the assistants that carry the Video Studio tool, defaulting
  // the way the "New presentation" dialog does (the user's own agent when it
  // can, else the General Assistant, else a company-managed one, else any).
  const makers = useToolAgents(VIDEO_TOOL_ID);
  const defaultMaker = makers.defaultAgentId ?? (makers.generalAssistantOffered ? GENERAL : null);
  const [makerPick, setMakerPick] = useState<string | null>(null);
  const maker =
    makerPick !== null && (makerPick === GENERAL || makers.agents.some((a) => a.id === makerPick)) ? makerPick : defaultMaker;
  const makerAgent = maker && maker !== GENERAL ? makers.agents.find((a) => a.id === maker) : undefined;
  const makerName = maker === GENERAL ? t_i18n("General Assistant") : (makerAgent?.name ?? t_i18n("your assistant"));

  useEffect(() => {
    if (!open) return;
    setMakerPick(null);
    setLooksOpen(false);
    setOptionsOpen(false);
    setChoices(NEW_VIDEO_CHOICES_UNSET);
  }, [open]);

  const ordered = useMemo(() => templatesForKind(rows, kind), [rows, kind]);
  const madeFor = (row: VideoTemplateSummary) => !!kind && isTemplatePurpose(kind) && row.purposes.includes(kind);
  const kindName = kind ? t_i18n(VIDEO_KIND_LABELS[kind]).toLowerCase() : "";

  const choiceLines = newVideoChoiceLines(choices, t_i18n);
  const choicesSet = newVideoChoicesSet(choices);
  const request = newVideoRequest(kind ? t_i18n(VIDEO_KIND_REQUESTS[kind]) : null, brief, choiceLines);
  const busy = createProject.isPending;
  const assistantBlocked = aiOff || makers.none;
  const canCreate = !isLoading && !busy && !assistantBlocked && !makers.resolving && maker !== null && !!brief.trim();

  const create = async (withAssistant: boolean) => {
    const title = withAssistant ? t_i18n("Untitled video") : t_i18n("New video");
    const look = pick === FREESTYLE ? { freestyle: true } : { template_id: pick };
    try {
      const video = await createProject.mutateAsync({
        title,
        ...look,
        ...newVideoSpecChoices(choices),
        format: { aspect_ratio: ratio },
        scenes: [{ type: "title", title }],
      });
      const launch: VideoEditorLaunch = withAssistant ? { send: request, agentId: maker === GENERAL ? null : maker } : {};
      onOpenChange(false);
      navigate(`/videos/${video.id}`, { state: launch });
    } catch (err) {
      toast({ title: t_i18n("The video was not created"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Clapperboard className="h-4 w-4 text-accent" />
            {t_i18n("New video")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Describe any video - for work or just for fun - and your assistant makes it with you: it plans the scenes, generates animated AI shots, writes the words and the narration, and records the product when there is one to show. You review, edit and render it in the editor.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>
              {t_i18n("What kind of video?")} <span className="font-normal text-default-secondary">{t_i18n("(optional)")}</span>
            </Label>
            <div className="grid gap-x-3 gap-y-1.5 sm:grid-cols-[auto_minmax(0,1fr)]">
              {VIDEO_KIND_GROUPS.map((group) => (
                <Fragment key={group.id}>
                  <span className="whitespace-nowrap content-caption text-default-secondary sm:pt-2">{t_i18n(VIDEO_KIND_GROUP_LABELS[group.id])}</span>
                  <div className="flex flex-wrap gap-1.5" role="group" aria-label={t_i18n(VIDEO_KIND_GROUP_LABELS[group.id])}>
                    {group.kinds.map((value) => (
                      <ChoiceChip key={value} selected={kind === value} onClick={() => chooseKind(value)} icon={KIND_ICONS[value]}>
                        {t_i18n(VIDEO_KIND_LABELS[value])}
                      </ChoiceChip>
                    ))}
                  </div>
                </Fragment>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="flex min-h-7 items-center gap-2">
              <Label htmlFor="vs-new-brief">{t_i18n("Describe it")}</Label>
              {kind && !aiOff && !brief.trim() && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-7 gap-1 content-compact"
                  onClick={() => setBrief(exampleBrief(t_i18n(VIDEO_KIND_EXAMPLES[kind])))}
                  data-testid="video.new.example"
                >
                  <Lightbulb className="h-3.5 w-3.5" />
                  {t_i18n("Use this example")}
                </Button>
              )}
            </div>
            <textarea
              id="vs-new-brief"
              rows={4}
              value={brief}
              disabled={aiOff}
              placeholder={kind ? t_i18n(VIDEO_KIND_EXAMPLES[kind]) : t_i18n("What the video is for, who watches it, how long, what it should show...")}
              onChange={(event) => setBrief(event.target.value)}
              className="w-full resize-y rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base placeholder:text-default-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>

          <div className="space-y-2">
            <div className="flex min-h-7 items-center gap-2">
              <Label>{t_i18n("Look")}</Label>
              <div className="flex min-w-0 flex-1 items-center gap-2 content-compact text-default-secondary" data-testid="video.new.look">
                {picked ? (
                  <>
                    <Swatches template={picked} className="h-1.5 w-10 shrink-0" />
                    <span className="truncate">
                      <span className="text-default-primary">{picked.name}</span>
                      {madeFor(picked) ? ` - ${t_i18n("made for {kind}", { kind: kindName })}` : ""}
                    </span>
                  </>
                ) : (
                  <>
                    <Palette className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">
                      <span className="text-default-primary">{t_i18n("Free style")}</span> - {t_i18n("no template, no brand, no added cards")}
                    </span>
                  </>
                )}
              </div>
              <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => setLooksOpen((o) => !o)} aria-expanded={looksOpen}>
                {looksOpen ? t_i18n("Done") : t_i18n("Change")}
              </Button>
            </div>
            {suggested && pick !== suggested.id && !looksOpen && (
              <p className="content-compact text-default-secondary">
                {t_i18n("Suggested for this kind of video: {template}.", { template: suggested.name })}{" "}
                <button type="button" className="text-accent underline-offset-2 hover:underline" onClick={() => choosePick(suggested.id)}>
                  {t_i18n("Use it")}
                </button>
              </p>
            )}
            {looksOpen &&
              (isLoading ? (
                <div className="h-24 animate-pulse rounded-sm bg-elevation-hover" />
              ) : (
                <div className="grid gap-2 sm:grid-cols-2">
                  <button
                    type="button"
                    onClick={() => choosePick(FREESTYLE)}
                    aria-pressed={pick === FREESTYLE}
                    className={cn(
                      "flex w-full flex-col rounded-sm border border-dashed p-3 text-left transition-colors",
                      pick === FREESTYLE ? "border-accent/50 bg-accent/5" : "border-elevation-subtle hover:bg-elevation-hover",
                    )}
                  >
                    <div className="flex items-center gap-2 content-base font-semibold">
                      <Palette className="h-3.5 w-3.5 text-default-secondary" />
                      {t_i18n("Free style")}
                    </div>
                    <p className="mt-2 content-compact text-default-secondary">{t_i18n("No template: no added cards, nothing locked. You choose the look in the editor.")}</p>
                  </button>
                  {ordered.map((row) => (
                    <TemplateChoice
                      key={row.id}
                      template={row}
                      selected={pick === row.id}
                      suggested={madeFor(row)}
                      onSelect={() => choosePick(row.id)}
                    />
                  ))}
                </div>
              ))}
          </div>

          <div className="space-y-1.5">
            <Label id="vs-new-format">{t_i18n("Format")}</Label>
            <div className="flex flex-wrap gap-1.5" role="group" aria-labelledby="vs-new-format">
              {ratios.map((value) => {
                const name = ASPECT_RATIO_LABELS[value as keyof typeof ASPECT_RATIO_LABELS];
                return (
                  <ChoiceChip key={value} selected={ratio === value} onClick={() => setRatio(value)}>
                    <span aria-hidden className={cn("shrink-0 rounded-sm border border-current", RATIO_SHAPES[value] ?? "h-3 w-3")} />
                    {name ? t_i18n(name) : value}
                    {name && <span className="text-default-secondary">{value}</span>}
                  </ChoiceChip>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex min-h-7 items-center gap-2">
              <Label>{t_i18n("More options")}</Label>
              <span className="min-w-0 flex-1 truncate content-compact text-default-secondary" data-testid="video.new.options-summary">
                {choicesSet
                  ? t_i18n("{count, plural, one {# choice set} other {# choices set}}", { count: choicesSet })
                  : t_i18n("Music, narration, captions, length and pace: the assistant suggests them")}
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 content-compact"
                onClick={() => setOptionsOpen((o) => !o)}
                aria-expanded={optionsOpen}
                aria-label={optionsOpen ? t_i18n("Done with the options") : t_i18n("Change the options")}
                data-testid="video.new.options-toggle"
              >
                {optionsOpen ? t_i18n("Done") : t_i18n("Change")}
              </Button>
            </div>
            {optionsOpen && <NewVideoOptions open={open} choices={choices} onChange={changeChoices} disabled={aiOff} />}
          </div>

          <div className="space-y-1.5">
            <Label className="flex items-center gap-1.5">
              <Bot className="h-3.5 w-3.5" />
              {t_i18n("Made by")}
            </Label>
            {makers.resolving ? (
              <div className="flex h-9 items-center gap-2 content-compact text-default-secondary">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Loading agents...")}
              </div>
            ) : makers.none ? (
              <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-3 py-2 content-caption leading-relaxed text-feedback-warning-tertiary">
                {t_i18n(
                  "No assistant can make videos yet. Turn on Video Studio in an agent's tools (or in the General Assistant's tools) and it will be offered here.",
                )}{" "}
                <Link to="/agents" className="text-accent underline-offset-2 hover:underline">
                  {t_i18n("Open agents")}
                </Link>
              </p>
            ) : (
              <SearchableSelect
                aria-label={t_i18n("Made by")}
                options={makers.agents.map((agent) => agentSelectOption(agent))}
                pinned={
                  makers.generalAssistantOffered
                    ? [{ value: GENERAL, label: t_i18n("General Assistant"), icon: <Sparkles className="h-3.5 w-3.5 text-accent" /> }]
                    : []
                }
                value={maker}
                onValueChange={setMakerPick}
                disabled={aiOff}
                placeholder={t_i18n("Search agents...")}
              />
            )}
          </div>

          {aiOff ? (
            <p className="rounded-sm border border-elevation-subtle bg-elevation-hover px-3 py-2 content-compact text-default-secondary">
              {t_i18n("AI is turned off on this platform: start from a blank video and build it by hand.")}
            </p>
          ) : (
            <div className="space-y-1.5 rounded-sm border border-elevation-subtle bg-elevation-hover p-3" data-testid="video.new.request">
              <p className="flex items-center gap-1.5 content-caption font-semibold uppercase tracking-wide text-default-secondary">
                <Send className="h-3 w-3" />
                {t_i18n("{assistant} receives", { assistant: makerName })}
              </p>
              {brief.trim() ? (
                <p className="whitespace-pre-wrap break-words content-compact text-default-primary">{request}</p>
              ) : (
                <p className="content-compact text-default-secondary">{t_i18n("Your brief, as you write it above.")}</p>
              )}
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Sent as your first message when the editor opens; the look, the format and the options you set are already on the video.")}
              </p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="ghost" onClick={() => void create(false)} disabled={isLoading || busy} className="gap-1.5">
              <FilePlus2 className="h-4 w-4" />
              {t_i18n("Start from a blank video")}
            </Button>
            <div className="ml-auto flex gap-2">
              <Button variant="ghost" onClick={() => onOpenChange(false)}>
                {t_i18n("Cancel")}
              </Button>
              <Button onClick={() => void create(true)} disabled={!canCreate} className="gap-1.5" aria-busy={busy || undefined}>
                {busy && <Spinner size="sm" tone="inherit" />}{!busy && <Sparkles className="h-4 w-4" />}
                {t_i18n("Create with the assistant")}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
