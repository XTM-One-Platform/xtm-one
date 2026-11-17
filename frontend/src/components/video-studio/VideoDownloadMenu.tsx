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
 * Download a video: its current render's MP4, captions and poster,
 * from the editor's header and the chat card (`video-downloads.ts` holds the
 * entries, shared with the library card and the render history). The current
 * render is what the video plays (`ready_render`); a video not rendered yet
 * says so instead of opening an empty menu.
 */
import { Download } from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoProject } from "@/types";

import { renderDownloadItems, useRenderDownload } from "./video-downloads";
import { Spinner } from "@filigran/design-system";

export function VideoDownloadMenu({
  video,
  compact = false,
  className,
}: {
  video: VideoProject;
  /** Icon-only trigger. */
  compact?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { download, isPending } = useRenderDownload();
  const render = video.ready_render;
  const label = t_i18n("Download");

  if (!render || render.status !== "ready") {
    // Drawn as `OverflowMenu`'s trigger, so the button keeps its look once a render exists.
    return (
      <Tip content={t_i18n("Render the video first to download it.")}>
        <span>
          <Button variant="ghost" className={cn("h-8 gap-1.5 px-2 content-compact", className)} disabled aria-label={label}>
            <Download className="h-4 w-4" />
            {!compact && <span>{label}</span>}
          </Button>
        </span>
      </Tip>
    );
  }
  return (
    <OverflowMenu
      items={renderDownloadItems(render, download, t_i18n)}
      label={t_i18n("Download the video")}
      className={className}
      busy={isPending}
      trigger={
        <>
          {isPending && <Spinner size="sm" tone="inherit" />}{!isPending && <Download className="h-4 w-4" />}
          {!compact && <span>{label}</span>}
        </>
      }
    />
  );
}
