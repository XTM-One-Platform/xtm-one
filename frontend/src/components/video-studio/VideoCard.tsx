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
 * The chat card of a ``[[VIDEO:<id>]]`` marker: the video's title, its
 * newest render's progress while it runs, and the studio's player once a
 * render is ready (its fullscreen takes the screen, or the window of
 * the embedding iframe), with its downloads. A video the reader cannot open (deleted, never shared) renders a
 * one-line notice instead of failing the conversation. In the editor's rail,
 * the card of the video on the stage is a one-line receipt
 * (`VideoCardContext`): no "Open" to the page it is already on, no second
 * player beside the preview.
 */
import { useContext } from "react";
import { Link } from "react-router-dom";
import { Clapperboard, ExternalLink } from "lucide-react";

import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { useVideoProject } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";

import { JobProgress, JobStatusBadge } from "./JobStatusBadge";
import { VideoDownloadMenu } from "./VideoDownloadMenu";
import { VideoPlayer } from "./VideoPlayer";
import { VideoCardContext } from "./video-card-context";
import { formatDuration, isUnfinished } from "./video-format";

export function VideoCard({ videoId }: { videoId: string }) {
  const { t_i18n } = useFormatter();
  const { studioVideoId } = useContext(VideoCardContext);
  const { data: video, isLoading, isError } = useVideoProject(videoId);
  if (isLoading) {
    return <div className="my-3 h-24 w-full max-w-2xl animate-pulse rounded-sm bg-elevation-hover" />;
  }
  if (isError || !video) {
    return <UnavailableCard message={t_i18n("This video is no longer available.")} />;
  }
  const latest = video.latest_render;
  const ready = video.ready_render;
  if (studioVideoId === video.id) {
    return (
      <div
        className="my-2 flex w-full max-w-2xl items-center gap-2 rounded-sm border border-elevation-subtle px-3 py-2 content-compact text-default-secondary"
        data-testid="video.card.receipt"
      >
        <Clapperboard className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate">
          {t_i18n("{title} - on the stage", { title: video.title })}
        </span>
        {latest && <JobStatusBadge status={latest.status} progress={latest.progress} />}
      </div>
    );
  }
  return (
    <div className="my-3 w-full max-w-2xl overflow-hidden rounded-sm border border-elevation-subtle bg-elevation-hover">
      <div className="flex items-center gap-3 px-4 py-3">
        <Clapperboard className="h-4 w-4 shrink-0 text-accent" />
        <div className="min-w-0 flex-1">
          <p className="truncate content-base font-semibold">{video.title}</p>
          <p className="content-compact text-default-secondary">
            {video.template ? `${video.template.name} - ` : `${t_i18n("Free style")} - `}
            {t_i18n("{count} scenes", { count: video.scene_count })}
            {" - "}
            {formatDuration(ready?.duration_ms ?? video.estimated_duration_ms)}
            {ready?.width ? ` - ${ready.width}x${ready.height} ${ready.fps} fps` : ""}
          </p>
        </div>
        {latest && <JobStatusBadge status={latest.status} progress={latest.progress} />}
        {ready?.status === "ready" && <VideoDownloadMenu video={video} compact className="h-7 w-7" />}
        <Link
          to={`/videos/${video.id}`}
          className="inline-flex items-center gap-1 content-compact text-default-secondary hover:text-default-primary"
        >
          {t_i18n("Open")}
          <ExternalLink className="h-3 w-3" />
        </Link>
      </div>
      {latest && isUnfinished(latest.status) && (
        <div className="px-4 pb-3">
          <JobProgress progress={latest.progress} stage={latest.stage} eta={latest.eta} />
        </div>
      )}
      {ready ? (
        <VideoPlayer
          kind="render"
          id={ready.id}
          hasCaptions={!!ready.captions_file_id && !ready.captions_burned_in}
          title={video.title}
          durationHint={ready.duration_ms ? ready.duration_ms / 1000 : undefined}
          chapters={ready.chapters}
          fps={ready.fps}
          className="rounded-none"
        />
      ) : (
        !latest && (
          <p className="px-4 pb-3 content-compact text-default-secondary">{t_i18n("Not rendered yet.")}</p>
        )
      )}
    </div>
  );
}
