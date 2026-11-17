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
 * A video opened by its public link - `/public/videos/:token`.
 *
 * The one video page with no session: the token in the address is the
 * credential, and the page holds nothing else. It plays the video's CURRENT
 * render - the MP4, its poster and, when they are not burned into the frame,
 * its captions - from the token-gated routes, which serve those three files
 * and nothing else. Nothing about who made the video, who it is shared with
 * or how it is built. `Download` appears only when the link's managers
 * allowed it; the server names the file and refuses it otherwise.
 *
 * A link that was turned off, replaced or never existed is one answer (404),
 * and the page says so without guessing which; a video not rendered yet says
 * that instead of showing an empty player.
 *
 * A rendered video takes the whole window: the page is the studio's
 * player, its title, the public mark and the download on top of the picture,
 * gone with the controls while it plays.
 */
import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, Captions, Clapperboard, Download, Film, Globe, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { CinemaPlayer } from "@/components/video-studio/CinemaPlayer";
import { formatDuration, formatSize } from "@/components/video-studio/video-format";
import { downloadPublicVideo, publicVideoMediaUrl, usePublicVideo } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

/** Anyone with the link watches: said on the title bar, the player's or the page's. */
function PublicMark({ container }: { container?: HTMLElement | null }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={t_i18n("Anyone with this link can watch this video")} container={container}>
      <span
        className="hidden shrink-0 items-center gap-1 rounded-sm border border-elevation-subtle-soft px-1.5 py-0.5 content-caption font-medium uppercase tracking-wide text-default-secondary sm:inline-flex"
        data-testid="video.public.badge"
      >
        <Globe className="h-3 w-3" aria-hidden="true" />
        {t_i18n("Public")}
      </span>
    </Tip>
  );
}

export default function VideoPublic() {
  const { token } = useParams<{ token: string }>();
  const { t_i18n, intl } = useFormatter();
  const { data: video, isLoading, isError, error, refetch } = usePublicVideo(token);

  // The video's title is the tab's title, as in the editor.
  useEffect(() => {
    if (!video?.title) return;
    const previous = document.title;
    document.title = video.title;
    return () => {
      document.title = previous;
    };
  }, [video?.title]);

  if (isLoading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-elevation-default">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !video || !token) {
    const gone = !error || error.status === 404;
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center" data-testid="video.public.gone">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base font-medium">
          {gone ? t_i18n("This link no longer opens a video.") : t_i18n("The video could not be loaded.")}
        </p>
        <p className="max-w-md content-base text-default-secondary">
          {gone
            ? t_i18n(
                "Whoever shared it may have turned the link off or replaced it with a new one, or the video was deleted. Ask them for a new link.",
              )
            : t_i18n("Check your connection and try again.")}
        </p>
        {!gone && (
          <Button variant="outline" size="sm" onClick={() => void refetch()} className="gap-1.5">
            <RefreshCw className="h-3.5 w-3.5" /> {t_i18n("Try again")}
          </Button>
        )}
      </div>
    );
  }

  const downloads: OverflowMenuItem[] = [
    {
      id: "video",
      label: video.size_bytes
        ? t_i18n("MP4 video ({size})", { size: formatSize(video.size_bytes) })
        : t_i18n("MP4 video"),
      icon: Film,
      onSelect: () => downloadPublicVideo(token, "video"),
    },
  ];
  if (video.has_captions) {
    downloads.push({
      id: "captions",
      label: t_i18n("Captions (WebVTT)"),
      icon: Captions,
      onSelect: () => downloadPublicVideo(token, "captions"),
    });
  }
  const showCaptions = video.has_captions && !video.captions_burned_in;
  const facts = [
    video.duration_ms ? formatDuration(video.duration_ms) : null,
    video.width && video.height ? `${video.width}x${video.height}` : null,
    video.rendered_at ? intl.formatDate(video.rendered_at, { dateStyle: "medium" }) : null,
  ].filter(Boolean);

  const downloadMenu = (className?: string) =>
    video.download && (
      <OverflowMenu
        items={downloads}
        inline
        label={t_i18n("Download the video")}
        className={cn("h-8 shrink-0 gap-1.5 px-2.5", className)}
        trigger={
          <>
            <Download className="h-4 w-4" />
            <span className="hidden content-compact sm:inline">{t_i18n("Download")}</span>
          </>
        }
      />
    );

  if (video.rendered) {
    return (
      <main className="h-dvh w-screen overflow-hidden" data-testid="video.public">
        <CinemaPlayer
          src={publicVideoMediaUrl(token, "video")}
          poster={video.has_poster ? publicVideoMediaUrl(token, "poster") : undefined}
          captions={showCaptions ? publicVideoMediaUrl(token, "captions") : undefined}
          title={video.title}
          layout="fill"
          fillsWindow
          autoFocus
          durationHint={video.duration_ms ? video.duration_ms / 1000 : undefined}
          chapters={video.chapters}
          fps={video.fps}
          // Where this visitor left this render of it: the link's first characters, never the whole credential.
          resumeKey={`public:${token.slice(0, 16)}:${video.rendered_at ?? ""}`}
          videoTestId="video.public.player"
          header={(container) => (
            <div className="flex items-center gap-2">
              <Clapperboard className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <h1 className="truncate content-base font-semibold text-on-scrim">{video.title}</h1>
                {facts.length > 0 && <p className="truncate content-compact tabular-nums text-on-scrim/70">{facts.join(" - ")}</p>}
              </div>
              <PublicMark container={container} />
              {downloadMenu("text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim")}
            </div>
          )}
        />
      </main>
    );
  }

  return (
    <div className="flex min-h-dvh flex-col bg-elevation-default text-default-primary" data-testid="video.public">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-elevation-subtle-soft px-4">
        <Clapperboard className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <h1 className="min-w-0 truncate content-base font-medium">{video.title}</h1>
        <PublicMark />
        {video.download && <div className="ml-auto">{downloadMenu()}</div>}
      </header>

      <main className="flex flex-1 flex-col items-center justify-center gap-3 p-4 sm:p-8">
        <div className="flex max-w-md flex-col items-center gap-3 text-center" data-testid="video.public.notRendered">
          <Clapperboard className="h-8 w-8 text-default-secondary" />
          <p className="content-base font-medium">{t_i18n("This video is not ready yet.")}</p>
          <p className="content-base text-default-secondary">
            {t_i18n("It has not been rendered yet. Come back to this link later: it plays the video once it is.")}
          </p>
        </div>
      </main>
    </div>
  );
}
