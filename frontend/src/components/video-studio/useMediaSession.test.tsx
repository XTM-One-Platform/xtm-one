// The system's media controls: once a video plays, the media keys and
// the lock screen drive it - play, pause, skips, seeking, chapters - and the
// player played last holds them; a player only gives up what it still holds.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { useMediaSession } from "./useMediaSession";

type Handler = ((details: MediaSessionActionDetails) => void) | null;

let handlers: Map<string, Handler>;
let session: { metadata: unknown; playbackState: string; setActionHandler: ReturnType<typeof vi.fn>; setPositionState: ReturnType<typeof vi.fn> };

beforeEach(() => {
  handlers = new Map();
  session = {
    metadata: null,
    playbackState: "none",
    setActionHandler: vi.fn((action: string, handler: Handler) => handlers.set(action, handler)),
    setPositionState: vi.fn(),
  };
  Object.defineProperty(navigator, "mediaSession", { configurable: true, value: session });
});

afterEach(() => {
  Object.defineProperty(navigator, "mediaSession", { configurable: true, value: undefined });
});

function videoOf(duration: number): HTMLVideoElement {
  const video = document.createElement("video");
  Object.defineProperty(video, "duration", { configurable: true, value: duration });
  return video;
}

describe("useMediaSession", () => {
  it("takes the controls only once the video plays", () => {
    const video = videoOf(64);
    renderHook(() => useMediaSession(video, { title: "Brand film", claim: 0, skipSeconds: 10 }));
    expect(session.setActionHandler).not.toHaveBeenCalled();
  });

  it("plays, pauses, skips and seeks the video, and reports where it is", () => {
    const video = videoOf(64);
    const view = renderHook(() => useMediaSession(video, { title: "Brand film", claim: 1, skipSeconds: 10 }));
    video.currentTime = 20;
    handlers.get("seekforward")?.({ action: "seekforward" });
    expect(video.currentTime).toBe(30);
    handlers.get("seekbackward")?.({ action: "seekbackward", seekOffset: 5 });
    expect(video.currentTime).toBe(25);
    handlers.get("seekto")?.({ action: "seekto", seekTime: 90 });
    expect(video.currentTime).toBe(64);
    video.dispatchEvent(new Event("timeupdate"));
    expect(session.setPositionState).toHaveBeenLastCalledWith({ duration: 64, playbackRate: 1, position: 64 });
    // Without chapters, the track keys are not offered.
    expect(handlers.get("nexttrack")).toBeNull();
    view.unmount();
    expect(handlers.get("play")).toBeNull();
    expect(session.playbackState).toBe("none");
  });

  it("hands the track keys to the chapters", () => {
    const onChapter = vi.fn();
    renderHook(() => useMediaSession(videoOf(64), { title: "Brand film", claim: 1, skipSeconds: 10, onChapter }));
    handlers.get("nexttrack")?.({ action: "nexttrack" });
    handlers.get("previoustrack")?.({ action: "previoustrack" });
    expect(onChapter.mock.calls).toEqual([[1], [-1]]);
  });

  it("leaves the controls to the player played last", () => {
    const first = videoOf(64);
    const second = videoOf(30);
    const one = renderHook(() => useMediaSession(first, { title: "One", claim: 1, skipSeconds: 10 }));
    renderHook(() => useMediaSession(second, { title: "Two", claim: 1, skipSeconds: 10 }));
    // The first one goes: the second keeps the controls.
    one.unmount();
    expect(handlers.get("play")).not.toBeNull();
    second.currentTime = 4;
    handlers.get("seekforward")?.({ action: "seekforward" });
    expect(second.currentTime).toBe(14);
    expect(first.currentTime).toBe(0);
  });
});
