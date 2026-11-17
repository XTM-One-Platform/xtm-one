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
 * The system's media controls for the Video Studio player: the
 * keyboard's media keys, the lock screen and the browser's media hub play,
 * pause and seek the video, under its title and poster, with its position.
 *
 * A page can hold several players (a conversation's video cards): the one
 * played last holds the controls. `claim` changes on every play, which takes
 * them back; a player only gives up the controls it still holds.
 */
import { useEffect } from "react";

let holder: symbol | null = null;

export function useMediaSession(
  video: HTMLVideoElement | null,
  {
    title,
    artwork,
    claim,
    skipSeconds,
    onChapter,
  }: {
    title: string;
    artwork?: string;
    /** `0` until the video first plays, then a new value at each play. */
    claim: number;
    skipSeconds: number;
    /** Next (`1`) or previous (`-1`) chapter, for the track keys; none without chapters. */
    onChapter?: (direction: 1 | -1) => void;
  },
) {
  useEffect(() => {
    const session = typeof navigator !== "undefined" ? navigator.mediaSession : undefined;
    if (!session || !video || !claim) return;
    const token = Symbol("video-player");
    holder = token;
    if (typeof MediaMetadata === "function") {
      session.metadata = new MediaMetadata({ title, artwork: artwork ? [{ src: artwork }] : [] });
    }
    const seek = (seconds: number) => {
      const end = Number.isFinite(video.duration) ? video.duration : seconds;
      video.currentTime = Math.min(Math.max(0, seconds), end);
    };
    const handlers: [MediaSessionAction, MediaSessionActionHandler | null][] = [
      ["play", () => void video.play()?.catch(() => undefined)],
      ["pause", () => video.pause()],
      ["seekbackward", (details) => seek(video.currentTime - (details.seekOffset ?? skipSeconds))],
      ["seekforward", (details) => seek(video.currentTime + (details.seekOffset ?? skipSeconds))],
      [
        "seekto",
        (details) => {
          if (typeof details.seekTime !== "number") return;
          if (details.fastSeek && typeof video.fastSeek === "function") video.fastSeek(details.seekTime);
          else seek(details.seekTime);
        },
      ],
      ["previoustrack", onChapter ? () => onChapter(-1) : null],
      ["nexttrack", onChapter ? () => onChapter(1) : null],
    ];
    const set = (action: MediaSessionAction, handler: MediaSessionActionHandler | null) => {
      try {
        session.setActionHandler(action, handler);
      } catch {
        // An action this browser does not know.
      }
    };
    handlers.forEach(([action, handler]) => set(action, handler));
    const position = () => {
      if (holder !== token || !(video.duration > 0) || !Number.isFinite(video.duration)) return;
      try {
        session.setPositionState?.({
          duration: video.duration,
          playbackRate: video.playbackRate || 1,
          position: Math.min(video.currentTime, video.duration),
        });
      } catch {
        // A position the browser refuses (a duration still settling): the next event sets it.
      }
    };
    const state = () => {
      if (holder === token) session.playbackState = video.paused ? "paused" : "playing";
    };
    const updates = ["timeupdate", "durationchange", "ratechange", "seeked"] as const;
    updates.forEach((name) => video.addEventListener(name, position));
    video.addEventListener("play", state);
    video.addEventListener("pause", state);
    state();
    position();
    return () => {
      updates.forEach((name) => video.removeEventListener(name, position));
      video.removeEventListener("play", state);
      video.removeEventListener("pause", state);
      if (holder !== token) return;
      holder = null;
      handlers.forEach(([action]) => set(action, null));
      session.metadata = null;
      session.playbackState = "none";
    };
  }, [video, title, artwork, claim, skipSeconds, onChapter]);
}
