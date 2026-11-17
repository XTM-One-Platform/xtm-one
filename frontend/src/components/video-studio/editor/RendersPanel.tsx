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
 * The editor's renders: produce the video at a quality, follow
 * the render, play and download what it made, cancel what runs. A render
 * reads the saved revision, so the editor saves its draft first; it replaces
 * the video's render still in progress, which the server cancels.
 *
 * Renders are kept under the render retention (`render_retention.py`):
 * the newest ready render of each quality, the CURRENT one - what the video
 * plays, in its cards, in chat and on its public link - and those kept by
 * hand, the others for the days an administrator set. Each row says what
 * keeps it and when the others go; the video's editors keep one, make one
 * current or delete one, and "Clean up renders" deletes many at once
 * (`RenderCleanupDialog`). Rendering is an edit (a render becomes what the video plays),
 * so a viewer is pointed to a copy of their own. A render's Play opens it in
 * the theater.
 *
 * A render is heard and watched back: each row lists what its checks
 * found, at its time in the MP4; a render its audio check failed keeps its
 * MP4 to play at those times. The quality gate may refuse a render (a
 * narration that failed its check, findings left open by this revision, a
 * scene changed since its preview): the reasons are listed and the editor
 * may render anyway, which the server records and audits.
 *
 * A render of an older revision still in progress is asked about once
 * before it is cancelled: the new render keeps the frames it drew of
 * the scenes the newer revision leaves unchanged.
 *
 * The video's scene previews are listed apart, each with the size it
 * was drawn at under it: a player scales a 720p draft to its display,
 * so its softness is the preview's, never the render's.
 */
import { useState } from "react";
import { Clapperboard, Download, Eraser, MonitorPlay, Pin, PinOff, Play, Square, Star, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { AgenticEEBadgeForButton, AgenticGateDialog } from "@/components/ui/license-gate-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  renderGateReasons,
  renderReplaceQuestion,
  useCancelRender,
  useDeleteRender,
  useRenderVideo,
  useScenePreviews,
  useUpdateRender,
  useVideoRenders,
} from "@/hooks/api/useVideoStudioApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import type {
  VideoProject,
  VideoRender,
  VideoRenderGateReason,
  VideoRenderQuality,
  VideoRenderReplaceQuestion,
  VideoRenderStarted,
} from "@/types";

import { JobProgress, JobStatusBadge } from "../JobStatusBadge";
import { VideoTheater } from "../VideoTheater";
import { RenderCleanupDialog } from "./RenderCleanupDialog";
import { renderDownloadItems, useRenderDownload } from "../video-downloads";
import {
  QUALITY_LABELS,
  findingKindLabel,
  findingMessage,
  formatClock,
  formatDuration,
  formatSize,
  isUnfinished,
  previewSize,
  renderCaption,
  renderGateReasonText,
} from "../video-format";
import { Section } from "./fields";
import { Spinner } from "@filigran/design-system";

/** What the retention keeps, in one sentence; `null` when it keeps everything. */
function retentionSentence(
  video: VideoProject,
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
): string | null {
  const rules = video.render_retention;
  if (!rules || (!rules.failed_cancelled_days && !rules.previews_days && !rules.superseded_ready_days)) return null;
  const forever = t_i18n("for good");
  const days = (count: number) => (count > 0 ? t_i18n("{count, plural, one {# day} other {# days}}", { count }) : forever);
  return t_i18n(
    "Failed and cancelled renders are kept {failed}, scene previews {previews} and older ready renders {superseded}. The newest ready render of each quality, the current render and the ones you keep are never deleted.",
    {
      failed: days(rules.failed_cancelled_days),
      previews: days(rules.previews_days),
      superseded: days(rules.superseded_ready_days),
    },
  );
}

/** Why the quality gate refused a render, one reason a line, and the choice to render anyway. */
function RenderGateDialog({
  reasons,
  onClose,
  onRenderAnyway,
}: {
  reasons: VideoRenderGateReason[] | null;
  onClose: () => void;
  onRenderAnyway: () => void;
}) {
  const { t_i18n, intl, locale } = useFormatter();
  const format = {
    timeAgo: (iso: string) => formatTimeAgo(intl, iso),
    dateTime: (iso: string) => formatShortDateTimeIntl(intl, iso),
    locale,
  };
  return (
    <Dialog open={reasons !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md" data-testid="video.editor.render.gate">
        <DialogHeader>
          <DialogTitle className="text-base">{t_i18n("Render it anyway?")}</DialogTitle>
          <DialogDescription>{t_i18n("The render was not started:")}</DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1 pl-5 content-compact">
          {(reasons ?? []).map((reason, index) => (
            <li key={`${index}-${reason.code}`}>{renderGateReasonText(reason, t_i18n, format)}</li>
          ))}
        </ul>
        <p className="content-caption text-default-secondary">
          {t_i18n("Rendering anyway is recorded on the render and in the audit log.")}
        </p>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button size="sm" onClick={onRenderAnyway} data-testid="video.editor.render.anyway">
            {t_i18n("Render anyway")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** What a render's checks found, each at its time in the MP4. */
function RenderFindings({ render }: { render: VideoRender }) {
  const { t_i18n } = useFormatter();
  const findings = render.checks?.findings ?? [];
  if (!findings.length) return null;
  return (
    <ul className="space-y-0.5 content-caption" data-testid="video.editor.render.findings">
      {findings.slice(0, 6).map((finding, index) => {
        const label = findingKindLabel(finding.kind);
        return (
          <li key={`${index}-${finding.kind}-${finding.at}`} className={finding.severity === "fail" ? "text-feedback-error-primary" : "text-default-secondary"}>
            <span className="tabular-nums">
              {formatClock(finding.at)}
              {finding.end !== null ? `-${formatClock(finding.end)}` : ""}
            </span>{" "}
            {label ? t_i18n(label) : finding.kind}
            {finding.scene_id ? ` (${finding.scene_id})` : ""}: {findingMessage(finding, t_i18n)}
          </li>
        );
      })}
      {findings.length > 6 && <li className="text-default-secondary">{t_i18n("and {count} more", { count: findings.length - 6 })}</li>}
    </ul>
  );
}

/** How the render's voice said each pronunciation lexicon term: passed of occurrences, what was heard, and
 * the takes it does not play that failed on it - a term a replaced take failed on never reads as all passed. */
function RenderPronunciation({ render }: { render: VideoRender }) {
  const { t_i18n } = useFormatter();
  const terms = render.checks?.pronunciation?.terms ?? [];
  if (!terms.length) return null;
  const heard = (forms: string[]) => t_i18n("heard {forms}", { forms: forms.map((form) => `\u201c${form}\u201d`).join(", ") });
  return (
    <p className="content-caption text-default-secondary" data-testid="video.editor.render.pronunciation">
      {t_i18n("Pronunciation")}:{" "}
      {terms.map((term, index) => {
        const failedTakes = term.attempts_failed ?? [];
        return (
          <span
            key={term.term}
            className={cn(
              term.passed < term.occurrences
                ? "text-feedback-error-primary"
                : failedTakes.length > 0 && "text-feedback-warning-primary",
            )}
          >
            {index > 0 ? ", " : ""}
            {term.term} {term.passed}/{term.occurrences}
            {term.passed < term.occurrences && term.heard.length > 0 ? ` (${heard(term.heard)})` : ""}
            {failedTakes.map((taken) => (
              <span key={taken.scene_id} data-testid="video.editor.render.pronunciation.failed-takes">
                {` \u00b7 ${t_i18n("{count, plural, one {# attempt failed} other {# attempts failed}} on scene {scene}", {
                  count: taken.times,
                  scene: taken.scene_id,
                })}`}
                {taken.heard.length > 0 ? ` (${heard(taken.heard)})` : ""}
              </span>
            ))}
          </span>
        );
      })}
    </p>
  );
}

/** How loud the render's music plays: where no one speaks, under the voice once ducked, its quietest stretch. */
function RenderMusicLevel({ render }: { render: VideoRender }) {
  const { t_i18n } = useFormatter();
  const music = render.checks?.audio?.music;
  if (!music) return null;
  const parts =
    music.integrated_lufs === null
      ? [t_i18n("silent")]
      : [
          t_i18n("{lufs} LUFS integrated", { lufs: music.integrated_lufs.toFixed(1) }),
          ...(music.under_voice_lufs !== null && music.below_voice_db !== null
            ? [
                t_i18n("{lufs} LUFS under the voice ({db} dB below it)", {
                  lufs: music.under_voice_lufs.toFixed(1),
                  db: music.below_voice_db.toFixed(0),
                }),
              ]
            : []),
          t_i18n("quietest stretch {seconds} s under {lufs} LUFS", {
            seconds: music.quiet_seconds.toFixed(1),
            lufs: music.quiet_lufs,
          }),
          ...(music.decay_seconds
            ? [
                t_i18n("the track decays {seconds} s under the closing card, by design", {
                  seconds: music.decay_seconds.toFixed(1),
                }),
              ]
            : []),
        ];
  return (
    <p
      className={cn("content-caption", music.status === "findings" ? "text-feedback-warning-primary" : "text-default-secondary")}
      data-testid="video.editor.render.music"
    >
      {t_i18n("Music")}: {parts.join(", ")}
    </p>
  );
}

function RenderRow({
  video,
  render,
  onPlay,
}: {
  video: VideoProject;
  render: VideoRender;
  onPlay: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const [ConfirmEl, confirm] = useConfirm();
  const cancel = useCancelRender();
  const update = useUpdateRender();
  const remove = useDeleteRender();
  const { download, isPending: downloading } = useRenderDownload();
  const ready = render.status === "ready";
  // A render its audio check failed keeps its MP4, to play at the times its findings give.
  const playable = ready || (render.status === "failed" && !!render.file_id);
  const mayDelete = video.can_manage || (!!user && render.user_id === user.id);

  const failed = (title: string) => (err: unknown) =>
    toast({ title, description: err instanceof Error ? err.message : undefined, variant: "error" });

  const setKeep = (keep: boolean) =>
    update.mutate(
      { renderId: render.id, keep },
      {
        onSuccess: () => toast({ title: keep ? t_i18n("Render kept") : t_i18n("Render no longer kept"), variant: "success" }),
        onError: failed(t_i18n("The change was not saved")),
      },
    );
  const makeCurrent = () =>
    update.mutate(
      { renderId: render.id, current: true },
      {
        onSuccess: () =>
          toast({
            title: t_i18n("Current render changed"),
            description: t_i18n("The video, its chat cards and its public link now play this render."),
            variant: "success",
          }),
        onError: failed(t_i18n("The change was not saved")),
      },
    );
  const onDelete = async () => {
    const ok = await confirm({
      title: t_i18n("Delete this render?"),
      description: render.is_current
        ? t_i18n("The MP4, its poster and captions are deleted. The newest other render becomes the one the video plays.")
        : t_i18n("The MP4, its poster and captions are deleted."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    remove.mutate(render.id, { onError: failed(t_i18n("The render was not deleted")) });
  };

  const items: OverflowMenuItem[] = ready ? renderDownloadItems(render, download, t_i18n) : [];
  if (video.can_manage && ready && !render.is_current) {
    items.push({ id: "current", label: t_i18n("Make it the current render"), icon: Star, onSelect: makeCurrent, separatorBefore: true });
  }
  if (video.can_manage && !isUnfinished(render.status)) {
    items.push(
      render.keep
        ? { id: "unkeep", label: t_i18n("Stop keeping it"), icon: PinOff, onSelect: () => setKeep(false), separatorBefore: !items.length || render.is_current }
        : { id: "keep", label: t_i18n("Keep it"), icon: Pin, onSelect: () => setKeep(true), separatorBefore: !items.length || render.is_current },
    );
  }
  if (mayDelete) {
    items.push({ id: "delete", label: t_i18n("Delete"), icon: Trash2, onSelect: () => void onDelete(), destructive: true, separatorBefore: true });
  }

  return (
    <div className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-2" data-testid="video.editor.render.row">
      <div className="flex flex-wrap items-center gap-1.5">
        <JobStatusBadge status={render.status} progress={render.progress} />
        <Badge variant="outline" className="whitespace-nowrap content-caption">
          {t_i18n(QUALITY_LABELS[render.quality])}
        </Badge>
        {render.is_current && (
          <Tip content={t_i18n("The render the video plays: in the library, in chat and on its public link.")}>
            <Badge variant="cyan" className="gap-1 content-caption" data-testid="video.editor.render.current">
              <Star className="h-2.5 w-2.5" /> {t_i18n("Current")}
            </Badge>
          </Tip>
        )}
        {render.keep && (
          <Tip content={t_i18n("Kept: the retention never deletes it.")}>
            <Badge variant="outline" className="gap-1 content-caption">
              <Pin className="h-2.5 w-2.5" /> {t_i18n("Kept")}
            </Badge>
          </Tip>
        )}
        <span className="ml-auto content-caption text-default-secondary">
          {t_i18n("revision {revision}", { revision: render.revision })}
          {render.created_at ? ` - ${formatTimeAgo(intl, render.created_at)}` : ""}
        </span>
      </div>
      {isUnfinished(render.status) && (
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <JobProgress progress={render.progress} stage={render.stage} eta={render.eta} />
          </div>
          <Button variant="ghost" size="sm" className="h-7 w-7 px-0" disabled={cancel.isPending} onClick={() => cancel.mutate(render.id)} aria-label={t_i18n("Cancel render")}>
            <Square className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
      {(playable || items.length > 0) && (
        <div className="flex items-center gap-1 text-[11px] text-default-secondary">
          {ready && (
            <span className="tabular-nums">
              {render.width}x{render.height} - {render.fps} fps - {formatDuration(render.duration_ms)} - {formatSize(render.size_bytes)}
            </span>
          )}
          <span className="ml-auto flex items-center">
            {playable && (
              <Tip content={t_i18n("Play it full screen")}>
                <Button variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={onPlay} aria-label={t_i18n("Play")}>
                  <Play className="h-3.5 w-3.5" />
                </Button>
              </Tip>
            )}
            {ready && (
              <>
                <Tip content={t_i18n("Download the MP4")}>
                  <Button variant="ghost" size="sm" className="h-7 w-7 px-0" disabled={downloading} onClick={() => download(render.id, "video")} aria-label={t_i18n("Download the MP4")} aria-busy={downloading || undefined}>
                    {downloading && <Spinner size="sm" tone="inherit" />}{!downloading && <Download className="h-3.5 w-3.5" />}
                  </Button>
                </Tip>
              </>
            )}
            {items.length > 0 && <OverflowMenu items={items} label={t_i18n("Render actions")} className="h-7 w-7" />}
          </span>
        </div>
      )}
      {!isUnfinished(render.status) && (render.kept_by === "latest_ready" || render.kept_by === "public_link") && (
        <p className="content-caption text-default-secondary" data-testid="video.editor.render.kept-by">
          {render.kept_by === "public_link"
            ? t_i18n("Kept: the public link plays it.")
            : t_i18n("Kept: the newest ready render at this quality.")}
        </p>
      )}
      {render.expires_at && !isUnfinished(render.status) && (
        <p className="content-caption text-default-secondary">
          {t_i18n("Kept until {date}", { date: intl.formatDate(render.expires_at, { dateStyle: "medium" }) })}
        </p>
      )}
      {render.status === "failed" && render.error && <p className="text-[11px] text-feedback-error-primary">{render.error}</p>}
      {render.forced && (
        <p className="content-caption text-default-secondary" data-testid="video.editor.render.forced">
          {t_i18n("Rendered past its quality gate.")}
        </p>
      )}
      <RenderFindings render={render} />
      <RenderPronunciation render={render} />
      <RenderMusicLevel render={render} />
      {render.status === "cancelled" && render.superseded_by_id && (
        <p className="content-caption text-default-secondary" data-testid="video.editor.render.superseded">
          {t_i18n("Replaced by a newer render.")}
        </p>
      )}
      {ready && render.notes.length > 0 && (
        <ul className="space-y-0.5 content-caption text-default-secondary">
          {render.notes.slice(0, 4).map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {ConfirmEl}
    </div>
  );
}

/** A scene preview: the scene it draws, how it went, and under it the size it was drawn at. */
function ScenePreviewRow({ render, onPlay }: { render: VideoRender; onPlay: () => void }) {
  const { t_i18n, intl } = useFormatter();
  const ready = render.status === "ready";
  return (
    <div className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-2" data-testid="video.editor.scene-preview">
      <div className="flex flex-wrap items-center gap-1.5">
        <JobStatusBadge status={render.status} progress={render.progress} />
        <span className="min-w-0 truncate content-compact font-medium">{t_i18n("Scene {scene}", { scene: render.scene_id ?? "" })}</span>
        <span className="ml-auto content-caption text-default-secondary">
          {t_i18n("revision {revision}", { revision: render.revision })}
          {render.created_at ? ` - ${formatTimeAgo(intl, render.created_at)}` : ""}
        </span>
      </div>
      {isUnfinished(render.status) && <JobProgress progress={render.progress} stage={render.stage} />}
      <div className="flex items-center gap-1 text-[11px] text-default-secondary">
        <span className="tabular-nums" data-testid="video.editor.scene-preview.size">
          {previewSize(render, t_i18n)}
        </span>
        {ready && (
          <span className="ml-auto flex items-center">
            <Tip content={t_i18n("Play it full screen")}>
              <Button variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={onPlay} aria-label={t_i18n("Play")}>
                <Play className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          </span>
        )}
      </div>
      {render.status === "failed" && render.error && <p className="text-[11px] text-feedback-error-primary">{render.error}</p>}
      <RenderFindings render={render} />
    </div>
  );
}

export function RendersPanel({
  video,
  onBeforeRender,
}: {
  video: VideoProject;
  /** Save the draft first: a render reads the saved revision. */
  onBeforeRender: () => Promise<boolean>;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [refused, setRefused] = useState<VideoRenderGateReason[] | null>(null);
  const [ConfirmEl, confirm] = useConfirm();
  const aiOff = useAiActionDisabled();
  const { data: renders } = useVideoRenders(video.id);
  const { data: previews } = useScenePreviews(video.id);
  const render = useRenderVideo();
  const [quality, setQuality] = useState<VideoRenderQuality>("high");
  const [playing, setPlaying] = useState<VideoRender | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const { user } = useAuth();
  const running = (renders ?? []).find((r) => isUnfinished(r.status));
  const retention = retentionSentence(video, t_i18n);
  // Who may delete something: the video's editors, or whoever asked for one of its renders or previews.
  const mayClean =
    (renders?.length ?? 0) + (previews?.length ?? 0) > 0 &&
    (video.can_manage || [...(renders ?? []), ...(previews ?? [])].some((r) => !!user && r.user_id === user.id));

  /** Whether to cancel an older revision's render still in progress; the newest of them is named. */
  const askToReplace = (question: VideoRenderReplaceQuestion): Promise<boolean> => {
    const older = question.renders[question.renders.length - 1];
    return confirm({
      title: t_i18n("Cancel the render in progress?"),
      description: t_i18n(
        "The render of revision {revision} is {progress}% done. Cancel it and render revision {next}? The frames it drew of the scenes this revision leaves unchanged are kept for the new render.",
        { revision: older?.revision ?? 0, progress: older?.progress ?? 0, next: question.revision },
      ),
      confirmLabel: t_i18n("Cancel it and render"),
      cancelLabel: t_i18n("Keep it drawing"),
      variant: "warning",
    });
  };

  const launch = async (force: boolean) => {
    let started: VideoRenderStarted;
    try {
      started = await render.mutateAsync({ projectId: video.id, quality, force });
    } catch (err) {
      const question = renderReplaceQuestion(err);
      if (!question) throw err;
      if (!(await askToReplace(question))) return;
      started = await render.mutateAsync({ projectId: video.id, quality, force, replace: true });
    }
    if (started.covered_from != null) {
      toast({
        title: t_i18n("Already rendered"),
        description: t_i18n("The latest render already shows this revision at this quality: nothing on screen or in the sound changed since."),
        variant: "info",
        action: { label: t_i18n("Render anyway"), onClick: () => void renderAnyway() },
      });
      return;
    }
    toast({
      title: t_i18n("Render started"),
      description: started.superseded_render_ids.length ? t_i18n("The render in progress was cancelled: this one replaces it.") : undefined,
      variant: "success",
    });
  };

  const start = async () => {
    if (!(await onBeforeRender())) {
      toast({ title: t_i18n("Fix the video first"), description: t_i18n("It has problems the render would refuse."), variant: "error" });
      return;
    }
    try {
      await launch(false);
    } catch (err) {
      // The quality gate: its reasons, and the choice to render anyway, recorded on the render.
      const reasons = renderGateReasons(err);
      if (reasons) setRefused(reasons);
      else toast({ title: t_i18n("The render did not start"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  const renderAnyway = async () => {
    setRefused(null);
    try {
      await launch(true);
    } catch (err) {
      toast({ title: t_i18n("The render did not start"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };

  return (
    <div data-testid="video.editor.renders">
      <Section title={t_i18n("Render")} icon={MonitorPlay}>
        {video.can_manage ? (
          <>
            <Select value={quality} onValueChange={(value) => setQuality(value as VideoRenderQuality)}>
              <SelectTrigger className="w-full" aria-label={t_i18n("Quality")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(QUALITY_LABELS) as VideoRenderQuality[]).map((key) => (
                  <SelectItem key={key} value={key}>
                    {t_i18n(QUALITY_LABELS[key])}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <AgenticGateDialog feature={t_i18n("Rendering videos")}>
              <Button
                className="h-8 w-full gap-1.5 content-compact"
                disabled={render.isPending || aiOff}
                onClick={() => void start()}
                data-testid="video.editor.render.start" aria-busy={render.isPending || undefined}>
                {render.isPending && <Spinner size="sm" tone="inherit" />}{!render.isPending && <MonitorPlay className="h-3.5 w-3.5" />}
                {video.ready_render || running ? t_i18n("Render again") : t_i18n("Render")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
            <p className="text-[11px] text-default-secondary">
              {running
                ? t_i18n("A render is in progress: starting a new one cancels it.")
                : t_i18n("The render speaks the narration, burns the captions and encodes the MP4 at this quality. It runs in the background: you can keep editing.")}
            </p>
          </>
        ) : (
          <p className="text-[11px] text-default-secondary">
            {t_i18n("Only the video's editors can render it. Duplicate it to render your own version.")}
          </p>
        )}
      </Section>

      <Section title={t_i18n("Render history")} icon={Play}>
        {retention && <p className="text-[11px] text-default-secondary">{retention}</p>}
        {mayClean && (
          <Button
            variant="secondary"
            size="sm"
            className="h-7 w-full gap-1.5 content-compact"
            onClick={() => setCleaning(true)}
            data-testid="video.editor.cleanup.open"
          >
            <Eraser className="h-3.5 w-3.5" /> {t_i18n("Clean up renders")}
          </Button>
        )}
        {!renders?.length && <p className="text-[11px] text-default-secondary">{t_i18n("Not rendered yet")}</p>}
        <div className="space-y-2">
          {(renders ?? []).map((r) => (
            <RenderRow key={r.id} video={video} render={r} onPlay={() => setPlaying(r)} />
          ))}
        </div>
      </Section>

      {!!previews?.length && (
        <Section title={t_i18n("Scene previews")} icon={Clapperboard} defaultOpen={false}>
          <p className="text-[11px] text-default-secondary">
            {t_i18n("A preview plays at the size it was drawn: a 720p draft looks softer on a large screen than the render will.")}
          </p>
          <div className="space-y-2">
            {previews.map((p) => (
              <ScenePreviewRow key={p.id} render={p} onPlay={() => setPlaying(p)} />
            ))}
          </div>
        </Section>
      )}

      {playing && (
        <VideoTheater
          media={{ kind: "render", render: playing }}
          title={video.title}
          subtitle={
            playing.scene_id
              ? `${t_i18n("Scene {scene}", { scene: playing.scene_id })} - ${previewSize(playing, t_i18n)}`
              : renderCaption(playing, t_i18n)
          }
          onClose={() => setPlaying(null)}
        />
      )}
      <RenderGateDialog reasons={refused} onClose={() => setRefused(null)} onRenderAnyway={() => void renderAnyway()} />
      {cleaning && <RenderCleanupDialog video={video} open={cleaning} onOpenChange={setCleaning} />}
      {ConfirmEl}
    </div>
  );
}
