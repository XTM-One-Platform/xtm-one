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
 * The video's music: one source at a time, its levels kept when it changes.
 *
 * - **Library**: the built-in tracks, composed by the platform for this video
 *   (its length, its scene changes, its final chord on the last frame); each
 *   one can be listened to (half a minute of its arc). No provider needed.
 * - **AI**: a track composed by a music model from a brief, at the video's
 *   length; its progress, its failure and a retry are shown here, and the
 *   person's other AI tracks can be reused.
 * - **Upload**: a sound file.
 *
 * The levels are the render's: the bed is levelled against the voice, so
 * `volume` is its level where no one speaks, and it goes down by `duck_db`
 * under every sentence.
 */
import { useEffect, useRef, useState } from "react";
import { Check, Pause, Play, RotateCcw, Sparkles, Trash2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { useAvailableMusicModels } from "@/hooks/api/useSettingsApi";
import {
  useCreateVideoMusic,
  useMusicLibrary,
  useRetryVideoMusic,
  useUploadVideoFile,
  useVideoMusic,
} from "@/hooks/api/useVideoStudioApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { musicTrackDescription, musicTrackWord } from "../video-format";
import { NumberField, SelectField, SwitchField, TextAreaField } from "./fields";
import { musicSourcePatch } from "./video-editor-model";
import { Spinner } from "@filigran/design-system";

type Patch = Record<string, unknown>;
type SourceKind = "track" | "music" | "file";
type Ending = "fade" | "natural";

const num = (value: unknown): number | undefined => (typeof value === "number" ? value : undefined);
const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);

/** What plays: the kind of source the music names (a prompt is an AI track on its way). */
function musicSourceKind(music: Record<string, unknown> | null): SourceKind | null {
  if (!music) return null;
  if (music.track) return "track";
  if (music.music_id || music.prompt) return "music";
  if (music.file_id) return "file";
  return null;
}

export function MusicSettings({
  music,
  plannedSeconds,
  canEdit,
  onChange,
}: {
  music: Record<string, unknown> | null;
  /** The video's planned length: an AI track is composed for it. */
  plannedSeconds: number | undefined;
  canEdit: boolean;
  onChange: (patch: Patch, coalesce?: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const off = !canEdit;
  const current = musicSourceKind(music);
  const [tab, setTab] = useState<SourceKind>(current ?? "track");
  useEffect(() => {
    if (current) setTab(current);
  }, [current]);
  const library = useMusicLibrary();
  const tracks = useVideoMusic(tab === "music" || current === "music");
  const models = useAvailableMusicModels(tab === "music");
  const compose = useCreateVideoMusic();
  const retry = useRetryVideoMusic();
  const upload = useUploadVideoFile();
  const fileInput = useRef<HTMLInputElement>(null);
  const audition = useRef<HTMLAudioElement>(null);
  const [listening, setListening] = useState<string | null>(null);
  const [brief, setBrief] = useState("");
  const aiTrack = music?.music_id ? (tracks.data ?? []).find((t) => t.id === music.music_id) : undefined;
  const readyTracks = (tracks.data ?? []).filter((t) => t.status === "ready");
  const set = (key: string) => (value: unknown) => onChange({ music: { [key]: value ?? null } }, `video.music.${key}`);

  useEffect(() => {
    const player = audition.current;
    return () => player?.pause();
  }, []);

  const listen = (id: string, url: string) => {
    const player = audition.current;
    if (!player) return;
    if (listening === id) {
      player.pause();
      setListening(null);
      return;
    }
    player.src = url;
    player.currentTime = 0;
    void player.play().then(
      () => setListening(id),
      () => setListening(null),
    );
  };

  const composeTrack = async () => {
    const prompt = brief.trim();
    if (!prompt) return;
    try {
      const seconds = Math.min(600, Math.max(5, Math.ceil((plannedSeconds ?? 56) + 4)));
      const made = await compose.mutateAsync({ prompt, duration_seconds: seconds, instrumental: true });
      onChange(musicSourcePatch(music, { music_id: made.music.id }));
      setBrief("");
    } catch (err) {
      toast({ title: t_i18n("The music could not be composed"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  const uploadSound = async (file: File | undefined) => {
    if (!file) return;
    try {
      const stored = await upload.mutateAsync(file);
      onChange(musicSourcePatch(music, { file_id: stored.file_id }));
    } catch (err) {
      toast({ title: t_i18n("Upload failed"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  return (
    <div className="space-y-3" data-testid="video.editor.music">
      <audio ref={audition} preload="none" className="hidden" onEnded={() => setListening(null)} />
      <SegmentedFilter
        ariaLabel={t_i18n("Music source")}
        value={tab}
        onChange={setTab}
        options={[
          { key: "track", label: "Library" },
          { key: "music", label: "AI music" },
          { key: "file", label: "Upload" },
        ]}
      />

      {tab === "track" && (
        <div className="space-y-1">
          <p className="content-caption text-default-secondary">
            {t_i18n("Royalty-free music the platform composes for this video: its length, its scene changes, a final chord on the last frame.")}
          </p>
          {library.isLoading && <Spinner size="sm" tone="inherit" className="text-accent" label={t_i18n("Loading")} />}
          <ul className="space-y-1">
            {(library.data ?? []).map((track) => {
              const chosen = music?.track === track.id;
              return (
                <li
                  key={track.id}
                  className={cn(
                    "flex items-start gap-2 rounded-sm border p-2",
                    chosen ? "border-accent/50 bg-accent/10" : "border-elevation-subtle-soft bg-elevation-default",
                  )}
                >
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 w-7 shrink-0 p-0"
                    aria-label={listening === track.id ? t_i18n("Stop listening") : t_i18n("Listen to {name}", { name: track.name })}
                    onClick={() => listen(track.id, track.audition_url)}
                  >
                    {listening === track.id ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  </Button>
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left disabled:cursor-not-allowed"
                    disabled={off}
                    onClick={() => onChange(musicSourcePatch(music, { track: track.id }))}
                  >
                    <span className="flex items-center gap-1.5 content-compact font-medium text-default-primary">
                      {track.name}
                      {chosen && <Check className="h-3 w-3 text-accent" aria-label={t_i18n("Chosen")} />}
                    </span>
                    <span className="block content-caption text-default-secondary">
                      {t_i18n(musicTrackWord(track.mood))} · {t_i18n(musicTrackWord(track.genre))} · {track.bpm} BPM
                    </span>
                    <span className="block content-caption text-default-secondary line-clamp-2">
                      {t_i18n(musicTrackDescription(track.id, track.description))}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {tab === "music" && (
        <div className="space-y-2">
          {aiTrack && (
            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-2 content-caption">
              <p className="font-medium text-default-primary line-clamp-2">{aiTrack.name || aiTrack.prompt}</p>
              {aiTrack.status === "ready" ? (
                <p className="text-default-secondary">
                  {t_i18n("Ready: {seconds} s with {model}", { seconds: Math.round((aiTrack.duration_ms ?? 0) / 1000), model: aiTrack.model })}
                </p>
              ) : aiTrack.status === "failed" || aiTrack.status === "cancelled" ? (
                <div className="space-y-1">
                  <p className="text-feedback-error-primary">{aiTrack.error || t_i18n("The AI track failed.")}</p>
                  <Button variant="outline" size="sm" className="h-7 gap-1 content-compact" disabled={off || aiOff || retry.isPending} onClick={() => retry.mutate(aiTrack.id)}>
                    <RotateCcw className="h-3 w-3" /> {t_i18n("Compose again")}
                  </Button>
                </div>
              ) : (
                <p className="flex items-center gap-1 text-default-secondary">
                  <Spinner size="sm" tone="inherit" /> {t_i18n("Composing ({progress}%)", { progress: aiTrack.progress })}
                </p>
              )}
            </div>
          )}
          {models.data && models.data.length === 0 ? (
            <p className="content-caption text-default-secondary">
              {t_i18n("No music model is configured: an admin adds one in Settings > AI and models > Media > Music generation. The library needs none.")}
            </p>
          ) : (
            <>
              <TextAreaField
                label={t_i18n("Describe the music")}
                value={brief}
                rows={3}
                maxLength={2000}
                placeholder={t_i18n("Uplifting electronic, warm pads and a plucked arpeggio, 112 BPM, builds to a confident finish, instrumental")}
                onChange={(value) => setBrief(value ?? "")}
                hint={t_i18n("Genre, mood, instruments, tempo, how it builds. Never an artist or a song name.")}
                disabled={off || aiOff}
              />
              <Button size="sm" className="h-7 gap-1.5 content-compact" disabled={off || aiOff || !brief.trim() || compose.isPending} onClick={() => void composeTrack()} aria-busy={compose.isPending || undefined}>
                {compose.isPending && <Spinner size="sm" tone="inherit" />}{!compose.isPending && <Sparkles className="h-3 w-3" />}
                {t_i18n("Compose for this video")}
              </Button>
            </>
          )}
          {readyTracks.length > 0 && (
            <SelectField
              label={t_i18n("Or one of your AI tracks")}
              value={str(music?.music_id) && readyTracks.some((t) => t.id === music?.music_id) ? str(music?.music_id) : undefined}
              unsetLabel={t_i18n("Choose a track")}
              options={readyTracks.map((t) => ({ value: t.id, label: (t.name || t.prompt).slice(0, 80) }))}
              onChange={(id) => id && onChange(musicSourcePatch(music, { music_id: id }))}
              disabled={off}
            />
          )}
        </div>
      )}

      {tab === "file" && (
        <div className="flex items-center gap-2">
          <input ref={fileInput} type="file" accept="audio/*" className="hidden" onChange={(e) => void uploadSound(e.target.files?.[0])} />
          <Button variant="outline" size="sm" className="h-7 gap-1.5 content-compact" disabled={off || upload.isPending} onClick={() => fileInput.current?.click()} aria-busy={upload.isPending || undefined}>
            {upload.isPending && <Spinner size="sm" tone="inherit" />}{!upload.isPending && <Upload className="h-3 w-3" />}
            {current === "file" ? t_i18n("Replace the music") : t_i18n("Upload a sound file")}
          </Button>
        </div>
      )}

      {music && (
        <div className="space-y-2 border-t border-elevation-subtle-soft pt-2">
          <NumberField
            label={t_i18n("Volume")}
            min={0}
            max={1}
            step={0.05}
            value={num(music.volume)}
            fallback={0.5}
            onChange={set("volume")}
            hint={t_i18n("Where no one speaks: 1 is as loud as the voice.")}
            disabled={off}
          />
          <SwitchField label={t_i18n("Lower it under the voice")} checked={music.ducking !== false} onChange={(v) => set("ducking")(v ? null : false)} disabled={off} />
          {music.ducking !== false && (
            <NumberField label={t_i18n("Lowered by")} unit="dB" min={3} max={30} step={1} value={num(music.duck_db)} fallback={10} onChange={set("duck_db")} disabled={off} />
          )}
          {current !== "track" && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <NumberField label={t_i18n("Start at")} unit="s" min={0} max={600} step={0.5} value={num(music.start)} fallback={0} onChange={set("start")} disabled={off} />
                <SelectField
                  label={t_i18n("Ending")}
                  value={str(music.ending) as Ending | undefined}
                  unsetLabel={t_i18n("Fade out")}
                  options={[{ value: "natural", label: t_i18n("The track's own end") }]}
                  onChange={set("ending")}
                  disabled={off}
                />
              </div>
              <SwitchField label={t_i18n("Repeat a shorter track")} checked={music.loop !== false} onChange={(v) => set("loop")(v ? null : false)} disabled={off} />
            </>
          )}
          <div className="grid grid-cols-2 gap-2">
            <NumberField label={t_i18n("Fade in")} unit="s" min={0} max={10} step={0.1} value={num(music.fade_in)} fallback={1} onChange={set("fade_in")} disabled={off} />
            <NumberField label={t_i18n("Fade out")} unit="s" min={0} max={15} step={0.1} value={num(music.fade_out)} fallback={2.5} onChange={set("fade_out")} disabled={off} />
          </div>
          <Button variant="ghost" size="sm" className="h-7 gap-1 content-compact" disabled={off} onClick={() => onChange({ music: null })}>
            <Trash2 className="h-3 w-3" /> {t_i18n("Remove the music")}
          </Button>
        </div>
      )}
    </div>
  );
}
