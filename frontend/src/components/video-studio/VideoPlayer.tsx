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
 * The Video Studio player on confined media URLs: a render, a
 * recording or an AI shot, its poster and its WebVTT captions when they are
 * not burned into the frame, minted for this reader (`useVideoMediaUrl`), in
 * the studio's own player (`CinemaPlayer`). Seeking is a byte-range request
 * the browser makes by itself; the server answers it from object storage.
 *
 * The player keeps the URL it started with while it answers: a token the
 * query refreshed meanwhile is taken only once that one stopped answering
 * (`onSourceLost`), so a video never restarts under its viewer. Where they
 * left it is remembered per take, clip or render (`resumeKey`).
 */
import { useState } from "react";

import { useRenewVideoMedia, useVideoMediaUrl } from "@/hooks/api/useVideoStudioApi";
import { cn } from "@/lib/utils";
import type { VideoMediaKind } from "@/types";

import { CinemaPlayer, type CinemaPlayerProps } from "./CinemaPlayer";

/** The URL a player plays: the first one minted, then a new one only once it was lost. */
function useHeldUrl(minted: string | undefined) {
  const [held, setHeld] = useState(minted);
  const [lost, setLost] = useState(false);
  if (minted && minted !== held && (held === undefined || lost)) {
    setHeld(minted);
    setLost(false);
  }
  return { url: held, lose: () => setLost(true) };
}

type VideoPlayerProps = {
  kind: VideoMediaKind;
  id: string;
  hasCaptions?: boolean;
} & Omit<CinemaPlayerProps, "src" | "poster" | "captions" | "onSourceLost">;

export function VideoPlayer(props: VideoPlayerProps) {
  // Another take, clip or render is another player: nothing held from the one before.
  return <ConfinedPlayer key={`${props.kind}:${props.id}`} {...props} />;
}

function ConfinedPlayer({ kind, id, hasCaptions = false, ...player }: VideoPlayerProps) {
  const video = useHeldUrl(useVideoMediaUrl(kind, id, "video"));
  const poster = useHeldUrl(useVideoMediaUrl(kind, id, "poster"));
  const captions = useHeldUrl(useVideoMediaUrl(kind, id, "captions", hasCaptions));
  const renew = useRenewVideoMedia(kind, id);
  return (
    <CinemaPlayer
      src={video.url}
      poster={poster.url}
      captions={hasCaptions ? captions.url : undefined}
      resumeKey={`${kind}:${id}`}
      onSourceLost={() => {
        video.lose();
        poster.lose();
        captions.lose();
        renew();
      }}
      {...player}
    />
  );
}

/** A poster still (render, take or clip) for a card; nothing while its URL is minted. `file`: the poster file it shows now. */
export function VideoPoster({
  kind,
  id,
  className,
  alt,
  file,
}: {
  kind: VideoMediaKind;
  id: string;
  className?: string;
  alt: string;
  file?: string | null;
}) {
  const poster = useVideoMediaUrl(kind, id, "poster", true, file);
  if (!poster) return <div className={cn("aspect-video w-full rounded-sm bg-elevation-hover", className)} />;
  return <img src={poster} alt={alt} className={cn("aspect-video w-full rounded-sm object-cover", className)} />;
}
