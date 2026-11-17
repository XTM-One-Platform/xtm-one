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
 * Downloading a render: the entries every download surface offers -
 * the editor's header and its render history, the library card, the chat card
 * - and the one call behind them. A download token names the file after the
 * video and its quality, and the MP4 streams to the disk.
 */
import { useCallback } from "react";
import { Captions, Film, ImageIcon } from "lucide-react";

import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useDownloadVideoAsset } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import type { VideoMediaAsset, VideoRender } from "@/types";

import { QUALITY_LABELS, formatSize } from "./video-format";

/** Download one file of a render, with the failure said in a toast. */
export function useRenderDownload() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const download = useDownloadVideoAsset();
  const run = useCallback(
    (renderId: string, asset: VideoMediaAsset = "video") =>
      download.mutate(
        { kind: "render", id: renderId, asset },
        {
          onError: (err) =>
            toast({
              title: t_i18n("The download did not start"),
              description: err instanceof Error ? err.message : undefined,
              variant: "error",
            }),
        },
      ),
    [download, t_i18n, toast],
  );
  return { download: run, isPending: download.isPending };
}

/** The menu entries for one render: the MP4 (quality and size), then what it has of captions and poster. */
export function renderDownloadItems(
  render: VideoRender,
  download: (renderId: string, asset: VideoMediaAsset) => void,
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
): OverflowMenuItem[] {
  const items: OverflowMenuItem[] = [
    {
      id: "download-video",
      label: t_i18n("MP4 video - {quality}, {size}", {
        quality: t_i18n(QUALITY_LABELS[render.quality]),
        size: formatSize(render.size_bytes),
      }),
      icon: Film,
      onSelect: () => download(render.id, "video"),
    },
  ];
  if (render.captions_file_id) {
    items.push({
      id: "download-captions",
      label: t_i18n("Captions (WebVTT)"),
      icon: Captions,
      onSelect: () => download(render.id, "captions"),
    });
  }
  if (render.poster_file_id) {
    items.push({
      id: "download-poster",
      label: t_i18n("Poster image (JPG)"),
      icon: ImageIcon,
      onSelect: () => download(render.id, "poster"),
    });
  }
  return items;
}
