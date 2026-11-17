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
 * The theater: Play opens a video over the whole window, on black,
 * and asks the browser's fullscreen in the same click - never a dialog box
 * around a small player. A render plays in it from the editor's bar, the
 * render history and the library; a recording or an AI shot from the media
 * library.
 *
 * It is a modal of its own rather than the library's dialog, which centres a
 * box, and a box is what this replaces. While it is open the page behind is
 * `inert` (the focus and the pointer stay in the theater) and the focus comes
 * back to what opened it. `Esc` leaves: the browser's fullscreen takes the
 * first one itself, and a menu or a tooltip of the player closes before the
 * theater does (their `Esc` arrives handled).
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Download, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { VideoRender } from "@/types";

import { VideoPlayer } from "./VideoPlayer";
import { renderDownloadItems, useRenderDownload } from "./video-downloads";
import { Spinner } from "@filigran/design-system";

export type TheaterMedia = { kind: "render"; render: VideoRender } | { kind: "take" | "clip"; id: string };

export function VideoTheater({
  media,
  title,
  subtitle,
  onClose,
}: {
  media: TheaterMedia;
  title: string;
  subtitle?: string;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { download, isPending } = useRenderDownload();
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  // Read while rendering: the player takes the focus in its own effect, which runs first.
  const [opener] = useState(() => (document.activeElement instanceof HTMLElement ? document.activeElement : null));
  const render = media.kind === "render" ? media.render : null;
  const id = media.kind === "render" ? media.render.id : media.id;

  useEffect(() => {
    if (!host) return;
    const behind = Array.from(document.body.children).filter((el): el is HTMLElement => el instanceof HTMLElement && el !== host && !el.inert);
    behind.forEach((el) => (el.inert = true));
    return () => {
      behind.forEach((el) => (el.inert = false));
      opener?.focus({ preventScroll: true });
    };
  }, [host, opener]);

  return createPortal(
    <div
      ref={setHost}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-[200]"
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        onClose();
      }}
      data-testid="video.theater"
    >
      <VideoPlayer
        kind={media.kind}
        id={id}
        hasCaptions={!!render?.captions_file_id && !render.captions_burned_in}
        title={title}
        layout="fill"
        fillsWindow
        autoPlay
        autoFocus
        startFullscreen
        pictureInPicture={false}
        durationHint={render?.duration_ms ? render.duration_ms / 1000 : undefined}
        chapters={render?.chapters}
        fps={render?.fps}
        videoTestId="video.theater.player"
        header={(container) => (
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1 pt-1">
              <p className="truncate content-base font-semibold text-on-scrim">{title}</p>
              {subtitle && <p className="truncate content-compact text-on-scrim/70">{subtitle}</p>}
            </div>
            {render && (
              <OverflowMenu
                inline
                items={renderDownloadItems(render, download, t_i18n)}
                label={t_i18n("Download the video")}
                busy={isPending}
                className="h-9 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim"
                trigger={
                  <>
                    {isPending && <Spinner size="sm" tone="inherit" />}{!isPending && <Download className="h-4 w-4" />}
                    <span className="hidden sm:inline">{t_i18n("Download")}</span>
                  </>
                }
              />
            )}
            <Tip content={`${t_i18n("Close")} (Esc)`} container={container}>
              <Button
                variant="ghost"
                size="sm"
                className="h-9 w-9 shrink-0 px-0 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim"
                onClick={onClose}
                aria-label={t_i18n("Close")}
                data-testid="video.theater.close"
              >
                <X className="h-5 w-5" />
              </Button>
            </Tip>
          </div>
        )}
      />
    </div>,
    document.body,
  );
}
