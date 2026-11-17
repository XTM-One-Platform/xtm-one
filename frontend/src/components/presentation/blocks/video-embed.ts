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
 * The external players a deck `video` block embeds - mirrors `video_embed` in
 * `presentation_spec.py`. Only YouTube (served from its privacy-enhanced
 * youtube-nocookie.com host) and Vimeo (player.vimeo.com) are framed; the
 * app's CSP `frame-src` allows exactly those two origins. Any other link is an
 * MP4 / WebM file the save imported as the block's `file_id`.
 */

type VideoProvider = "youtube" | "vimeo";

export interface VideoEmbed {
  provider: VideoProvider;
  id: string;
}

const HOSTS: Record<VideoProvider, readonly string[]> = {
  youtube: ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"],
  vimeo: ["vimeo.com", "www.vimeo.com", "player.vimeo.com"],
};
const YOUTUBE_ID = /^[A-Za-z0-9_-]{11}$/;
const VIMEO_ID = /^[0-9]{1,12}$/;

/** The provider and video of a YouTube or Vimeo https link; `null` for any other link. */
export function videoEmbed(url: string | null | undefined): VideoEmbed | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:") return null;
  const host = parsed.hostname.toLowerCase();
  const provider = (Object.keys(HOSTS) as VideoProvider[]).find((name) => HOSTS[name].includes(host));
  if (!provider) return null;
  const segments = parsed.pathname.split("/").filter(Boolean);
  if (provider === "youtube") {
    let id = "";
    if (host === "youtu.be") id = segments[0] ?? "";
    else if (segments[0] === "watch") id = parsed.searchParams.get("v") ?? "";
    else if (segments.length >= 2 && ["embed", "shorts", "live", "v"].includes(segments[0])) id = segments[1];
    return YOUTUBE_ID.test(id) ? { provider, id } : null;
  }
  const id = host === "player.vimeo.com" ? (segments[0] === "video" ? (segments[1] ?? "") : "") : (segments.find((s) => VIMEO_ID.test(s)) ?? "");
  return VIMEO_ID.test(id) ? { provider, id } : null;
}

interface PlayerOptions {
  autoplay?: boolean;
  muted?: boolean;
  loop?: boolean;
  controls?: boolean;
  start?: number | null;
  end?: number | null;
}

/** The player address of *embed*, on the provider's embed origin, with the block's playback options. */
export function videoEmbedSrc(embed: VideoEmbed, options: PlayerOptions = {}): string {
  const start = typeof options.start === "number" && options.start > 0 ? Math.floor(options.start) : null;
  const end = typeof options.end === "number" && options.end > 0 ? Math.floor(options.end) : null;
  if (embed.provider === "youtube") {
    const params = new URLSearchParams({ rel: "0", playsinline: "1" });
    if (options.autoplay) params.set("autoplay", "1");
    if (options.muted) params.set("mute", "1");
    if (options.controls === false) params.set("controls", "0");
    if (options.loop) {
      params.set("loop", "1");
      params.set("playlist", embed.id);
    }
    if (start !== null) params.set("start", String(start));
    if (end !== null) params.set("end", String(end));
    return `https://www.youtube-nocookie.com/embed/${embed.id}?${params.toString()}`;
  }
  const params = new URLSearchParams({ dnt: "1" });
  if (options.autoplay) params.set("autoplay", "1");
  if (options.muted) params.set("muted", "1");
  if (options.loop) params.set("loop", "1");
  if (options.controls === false) params.set("controls", "0");
  return `https://player.vimeo.com/video/${embed.id}?${params.toString()}${start !== null ? `#t=${start}s` : ""}`;
}

/** The video's own page, the link the PDF and the PPTX put under its poster. */
export function videoEmbedPage(embed: VideoEmbed): string {
  return embed.provider === "youtube" ? `https://www.youtube.com/watch?v=${embed.id}` : `https://vimeo.com/${embed.id}`;
}
