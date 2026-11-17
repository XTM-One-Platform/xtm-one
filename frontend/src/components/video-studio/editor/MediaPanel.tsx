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
 * The editor's media bin: everything a scene can be made of, one
 * click from the timeline - statements, title and closing cards, AI shots
 * (generated right here, with the video models of the platform's providers,
 * and placed while they generate), recordings, images and video files uploaded
 * from the computer.
 *
 * Media are the video's: the recordings and shots its scenes use are
 * listed - read by id, whatever their age - and the rest of the
 * library is found by a search the server runs, never listed whole.
 * A failed shot is not media: it shows in its scene, with why and a way to
 * generate it again, and is purged once no video uses it. A recording or a
 * shot no video uses shows its media retention: when the files expire; the
 * ones a video uses never do.
 */
import { useMemo, useRef, useState } from "react";
import { Film, ImageIcon, Library, MonitorPlay, Plus, Quote, Search, Sparkles, Type, Upload, Wand2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AgenticEEBadgeForButton, AgenticGateDialog } from "@/components/ui/license-gate-dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useAvailableVideoModels } from "@/hooks/api/useSettingsApi";
import { useCreateVideoClip, useUploadVideoFile, useVideoAssets } from "@/hooks/api/useVideoStudioApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoAsset, VideoClip, VideoTake } from "@/types";

import { JobStatusBadge } from "../JobStatusBadge";
import { VideoPoster } from "../VideoPlayer";
import { formatDuration, isUnfinished, retentionDays } from "../video-format";
import { recordDemoPrompt } from "../video-handoff";
import { Field, Section, TextAreaField } from "./fields";
import { blankScene, type EditorScene } from "./video-editor-model";
import { Spinner } from "@filigran/design-system";

const PLATFORM = "__platform__";
const SHOT_SECONDS = ["4", "6", "8", "10", "12", "16", "20"] as const;
/** The library rows shown at once: the most recent, or the first that match the search. */
const LIBRARY_PAGE = 8;
/** The most media a video's own list reads at once (the server's page limit). */
const VIDEO_MEDIA_LIMIT = 100;

type MediaItem = { kind: "take"; item: VideoTake; title: string } | { kind: "clip"; item: VideoClip; title: string };

function mediaItem(asset: VideoAsset): MediaItem {
  return asset.kind === "take"
    ? { kind: "take", item: asset, title: asset.name }
    : { kind: "clip", item: asset, title: asset.name || asset.prompt };
}

/**
 * A recording's or a shot's media retention. *inVideo*: the video being edited
 * uses it, saved or not yet, so it never expires.
 */
export function RetentionBadge({ item, inVideo = false }: { item: VideoTake | VideoClip; inVideo?: boolean }) {
  const { t_i18n } = useFormatter();
  if (item.expired_at) return <Badge variant="destructive" className="content-caption">{t_i18n("Media expired")}</Badge>;
  if (item.keep) return <Badge variant="cyan" className="content-caption">{t_i18n("Kept")}</Badge>;
  const days = inVideo ? null : retentionDays(item.expires_at);
  if (days === null) return null;
  return (
    <Tip content={t_i18n("No video uses it: the media retention deletes its files then, unless a video uses it again. The videos already rendered keep theirs.")}>
      <Badge variant={days <= 3 ? "warning" : "outline"} className="content-caption">
        {days === 0 ? t_i18n("Expires today") : t_i18n("Expires in {days, plural, one {# day} other {# days}}", { days })}
      </Badge>
    </Tip>
  );
}

function MediaRow({
  media: { kind, item, title },
  onAdd,
  disabled,
  inVideo = false,
}: {
  media: MediaItem;
  onAdd: () => void;
  disabled: boolean;
  inVideo?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const usable = !item.expired_at;
  return (
    <div className="flex items-center gap-2 rounded-sm p-1 hover:bg-elevation-hover">
      <div className="relative w-16 shrink-0 overflow-hidden rounded-sm">
        {item.poster_file_id ? (
          <VideoPoster kind={kind} id={item.id} file={item.poster_file_id} alt="" className="rounded-none" />
        ) : (
          <div className="flex aspect-video w-full items-center justify-center bg-elevation-hover">
            {kind === "take" ? <MonitorPlay className="h-3.5 w-3.5 text-default-secondary" /> : <Wand2 className="h-3.5 w-3.5 text-default-secondary" />}
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate content-compact font-medium">{title}</p>
        <div className="mt-0.5 flex flex-wrap items-center gap-1 content-caption text-default-secondary">
          {item.duration_ms ? <span className="tabular-nums">{formatDuration(item.duration_ms)}</span> : null}
          {isUnfinished(item.status) ? <JobStatusBadge status={item.status} progress={item.progress} /> : null}
          <RetentionBadge item={item} inVideo={inVideo} />
        </div>
      </div>
      <Tip content={t_i18n("Add to the video")}>
        <Button variant="ghost" size="sm" className="h-7 w-7 shrink-0 px-0" onClick={onAdd} disabled={disabled || !usable} aria-label={t_i18n("Add to the video")}>
          <Plus className="h-3.5 w-3.5" />
        </Button>
      </Tip>
    </div>
  );
}

/** The scene a recording or a shot of the library makes. */
function sceneOf(media: MediaItem): EditorScene {
  return media.kind === "take" ? { type: "recording", take_id: media.item.id } : { type: "clip", clip_id: media.item.id };
}

export function MediaPanel({
  canEdit,
  aspectRatio,
  inVideo,
  onInsert,
  onAskAssistant,
}: {
  canEdit: boolean;
  aspectRatio: string;
  /** The recordings and shots the video's scenes use (`videoMedia`). */
  inVideo: { takes: Set<string>; clips: Set<string> };
  /** Put a scene in the video, after the selected one. */
  onInsert: (scene: EditorScene) => void;
  /** Hand a sentence to the rail's composer (a recording is the assistant's job: it writes the script). */
  onAskAssistant: (prompt: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const { data: videoModels } = useAvailableVideoModels();
  const createClip = useCreateVideoClip();
  const upload = useUploadVideoFile();
  const imageInput = useRef<HTMLInputElement>(null);
  const videoInput = useRef<HTMLInputElement>(null);
  const [prompt, setPrompt] = useState("");
  const [seconds, setSeconds] = useState<(typeof SHOT_SECONDS)[number]>("8");
  const [model, setModel] = useState<string>(PLATFORM);
  const [search, setSearch] = useState("");
  const searched = useDebounce(search, 250).trim();
  const off = !canEdit;

  // Sorted, so the same media make the same query whatever the scenes' order.
  const ownIds = useMemo(() => [...inVideo.takes, ...inVideo.clips].sort(), [inVideo]);
  const own = useVideoAssets({ ids: ownIds, limit: VIDEO_MEDIA_LIMIT, offset: 0 }, ownIds.length > 0);
  const others = useVideoAssets({ search: searched || undefined, exclude: ownIds, available: true, limit: LIBRARY_PAGE, offset: 0 });
  const here = useMemo(() => (ownIds.length ? (own.data?.items ?? []).map(mediaItem) : []), [own.data, ownIds.length]);
  const shown = useMemo(() => (others.data?.items ?? []).map(mediaItem), [others.data]);
  const found = others.data?.total ?? 0;

  const uploadScene = async (file: File | undefined, kind: "image" | "clip") => {
    if (!file) return;
    try {
      const stored = await upload.mutateAsync(file);
      onInsert(kind === "image" ? { type: "image", file_id: stored.file_id } : { type: "clip", file_id: stored.file_id });
    } catch (err) {
      toast({ title: t_i18n("Upload failed"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  const generate = async () => {
    const text = prompt.trim();
    if (!text) return;
    const [providerId, modelName] = model === PLATFORM ? [undefined, undefined] : model.split("::");
    try {
      const { clip } = await createClip.mutateAsync({
        prompt: text,
        duration_seconds: Number(seconds),
        aspect_ratio: aspectRatio === "9:16" ? "9:16" : "16:9",
        provider_id: providerId,
        model: modelName,
      });
      onInsert({ type: "clip", clip_id: clip.id });
      setPrompt("");
      toast({
        title: t_i18n("The AI shot is being generated"),
        description: t_i18n("It is in the video already; the preview shows it once it is ready, in a few minutes."),
        variant: "success",
      });
    } catch (err) {
      toast({ title: t_i18n("The shot did not start"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  return (
    <div data-testid="video.editor.media">
      <Section title={t_i18n("Cards")} icon={Type}>
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" className="col-span-2 h-8 gap-1.5 content-compact" disabled={off} onClick={() => onInsert(blankScene("statement", t_i18n("Your *message* here")))}>
            <Quote className="h-3.5 w-3.5" /> {t_i18n("Statement")}
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1.5 content-compact" disabled={off} onClick={() => onInsert(blankScene("title", t_i18n("New title")))}>
            <Type className="h-3.5 w-3.5" /> {t_i18n("Title card")}
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1.5 content-compact" disabled={off} onClick={() => onInsert(blankScene("outro", t_i18n("Thank you")))}>
            <Type className="h-3.5 w-3.5" /> {t_i18n("Closing card")}
          </Button>
          <Button variant="outline" size="sm" className="col-span-2 h-8 gap-1.5 content-compact" disabled={off} onClick={() => onInsert(blankScene("motion", t_i18n("Your *message* here")))}>
            <Sparkles className="h-3.5 w-3.5" /> {t_i18n("Motion graphics")}
          </Button>
        </div>
        <p className="text-[11px] text-default-secondary">{t_i18n("A statement is a message in big type, revealed line by line - the voice of brand films and booth loops.")}</p>
        <p className="text-[11px] text-default-secondary">{t_i18n("Motion graphics move in the video's motion personality: a kinetic title, a counted figure, a list built item by item, a logo reveal.")}</p>
      </Section>

      <Section title={t_i18n("AI shot")} icon={Sparkles}>
        <TextAreaField
          label={t_i18n("Describe the shot")}
          value={prompt}
          rows={3}
          maxLength={4000}
          placeholder={t_i18n("A slow dolly shot over a glowing network map at night, cinematic light...")}
          onChange={(value) => setPrompt(value ?? "")}
          disabled={off || aiOff}
        />
        <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
          <Field label={t_i18n("Length")}>
            <Select value={seconds} onValueChange={(v) => setSeconds(v as (typeof SHOT_SECONDS)[number])} disabled={off || aiOff}>
              <SelectTrigger className="w-full" aria-label={t_i18n("Length")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SHOT_SECONDS.map((value) => (
                  <SelectItem key={value} value={value}>
                    {value} s
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label={t_i18n("Video model")}>
            <SearchableSelect
              aria-label={t_i18n("Video model")}
              options={(videoModels ?? [])
                .filter((m) => m.provider_id)
                .map((m) => ({
                  value: `${m.provider_id}::${m.model}`,
                  label: m.model,
                  description: m.provider,
                  selectedLabel: `${m.model} - ${m.provider}`,
                }))}
              pinned={[{ value: PLATFORM, label: t_i18n("The platform's video model") }]}
              value={model}
              onValueChange={setModel}
              disabled={off || aiOff}
              placeholder={t_i18n("Search models...")}
            />
          </Field>
        </div>
        <AgenticGateDialog feature={t_i18n("Generating AI shots")}>
          <Button className="h-8 w-full gap-1.5 content-compact" disabled={off || aiOff || !prompt.trim() || createClip.isPending} onClick={() => void generate()} aria-busy={createClip.isPending || undefined}>
            {createClip.isPending && <Spinner size="sm" tone="inherit" />}{!createClip.isPending && <Wand2 className="h-3.5 w-3.5" />}
            {t_i18n("Generate and add")}
            <AgenticEEBadgeForButton />
          </Button>
        </AgenticGateDialog>
      </Section>

      <Section title={t_i18n("Recordings")} icon={MonitorPlay}>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full gap-1.5 content-compact"
          disabled={aiOff}
          onClick={() => onAskAssistant(recordDemoPrompt())}
        >
          <Sparkles className="h-3.5 w-3.5 text-accent" /> {t_i18n("Record a new flow with the assistant")}
        </Button>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Recordings are made in the platform's own browser from a step script: ask the assistant beside the stage.")}
        </p>
      </Section>

      <Section title={t_i18n("In this video")} icon={Film}>
        {here.length === 0 ? (
          <p className="text-[11px] text-default-secondary">
            {t_i18n("No recording or AI shot in this video yet: generate one above, or add one from your library below.")}
          </p>
        ) : (
          <div className="space-y-0.5" data-testid="video.editor.media.in-video">
            {here.map((media) => (
              <MediaRow key={`${media.kind}-${media.item.id}`} media={media} inVideo disabled={off} onAdd={() => onInsert(sceneOf(media))} />
            ))}
          </div>
        )}
      </Section>

      <Section title={t_i18n("Your library")} icon={Library}>
        {found === 0 && !searched ? (
          <p className="text-[11px] text-default-secondary">{t_i18n("Your other recordings and AI shots show here, to use them in this video too.")}</p>
        ) : (
          <>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-default-secondary" />
              <Input
                aria-label={t_i18n("Search your recordings and AI shots")}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={t_i18n("Search your recordings and AI shots")}
                className="h-8 pl-7 content-compact"
              />
            </div>
            {shown.length === 0 ? (
              <p className="text-[11px] text-default-secondary">{t_i18n("Nothing in your library matches.")}</p>
            ) : (
              <div className="space-y-0.5" data-testid="video.editor.media.library">
                {shown.map((media) => (
                  <MediaRow key={`${media.kind}-${media.item.id}`} media={media} disabled={off} onAdd={() => onInsert(sceneOf(media))} />
                ))}
              </div>
            )}
            {found > shown.length && (
              <p className="text-[11px] text-default-secondary">
                {t_i18n("{shown} of {count}: search to find the others.", { shown: shown.length, count: found })}
              </p>
            )}
          </>
        )}
      </Section>

      <Section title={t_i18n("From your computer")} icon={Upload}>
        <input ref={imageInput} type="file" accept="image/*" className="hidden" onChange={(e) => void uploadScene(e.target.files?.[0], "image")} />
        <input ref={videoInput} type="file" accept="video/*" className="hidden" onChange={(e) => void uploadScene(e.target.files?.[0], "clip")} />
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" className={cn("h-8 gap-1.5 content-compact")} disabled={off || upload.isPending} onClick={() => imageInput.current?.click()} aria-busy={upload.isPending || undefined}>
            {upload.isPending && <Spinner size="sm" tone="inherit" />}{!upload.isPending && <ImageIcon className="h-3.5 w-3.5" />} {t_i18n("Image")}
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1.5 content-compact" disabled={off || upload.isPending} onClick={() => videoInput.current?.click()} aria-busy={upload.isPending || undefined}>
            {upload.isPending && <Spinner size="sm" tone="inherit" />}{!upload.isPending && <MonitorPlay className="h-3.5 w-3.5" />} {t_i18n("Video file")}
          </Button>
        </div>
        <p className="text-[11px] text-default-secondary">{t_i18n("An event film or a keynote: upload it, then keep its best seconds with Starts at / Ends at in the scene.")}</p>
      </Section>
    </div>
  );
}
