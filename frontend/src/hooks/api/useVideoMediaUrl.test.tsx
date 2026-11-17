// A player's media token is minted again before it expires and asked once more
// on a 404: the studio's player never blinks to its backdrop while a
// URL is minted between two clips.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import type { VideoMediaToken } from "@/types";

import {
  MEDIA_TOKEN_REFRESH_MARGIN_MS,
  mediaTokenFreshFor,
  mediaTokenRefreshIn,
  retryMediaToken,
  useVideoMediaUrl,
} from "./useVideoStudioApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const NOW = Date.UTC(2026, 9, 20, 12, 0, 0);
const token = (url: string, expiresInMs: number, now = NOW): VideoMediaToken => ({
  url,
  expires_at: Math.floor((now + expiresInMs) / 1000),
  content_type: "video/mp4",
});
const notFound = () => Object.assign(new Error("Not found"), { status: 404 });

beforeEach(() => {
  post.mockReset();
});

describe("media token timing", () => {
  it("is minted again five minutes before it expires", () => {
    const minted = token("/media/a", 45 * 60 * 1000);
    expect(mediaTokenRefreshIn(minted, NOW)).toBe(45 * 60 * 1000 - MEDIA_TOKEN_REFRESH_MARGIN_MS);
    expect(mediaTokenFreshFor(minted, NOW)).toBe(40 * 60 * 1000);
    // Fetched ten minutes ago: fresh for thirty more.
    expect(mediaTokenFreshFor(minted, NOW - 10 * 60 * 1000)).toBe(50 * 60 * 1000);
  });

  it("is minted again soon, never in a loop, once it is about to expire or expired", () => {
    expect(mediaTokenRefreshIn(token("/media/a", 60 * 1000), NOW)).toBe(30 * 1000);
    expect(mediaTokenRefreshIn(token("/media/a", -60 * 1000), NOW)).toBe(30 * 1000);
    expect(mediaTokenFreshFor(token("/media/a", -60 * 1000), NOW)).toBe(0);
    expect(mediaTokenRefreshIn(undefined, NOW)).toBe(false);
  });

  it("asks a 404 once more, and nothing else", () => {
    expect(retryMediaToken(0, notFound())).toBe(true);
    expect(retryMediaToken(1, notFound())).toBe(false);
    expect(retryMediaToken(0, Object.assign(new Error("boom"), { status: 500 }))).toBe(false);
    expect(retryMediaToken(0, new Error("network"))).toBe(false);
  });
});

describe("useVideoMediaUrl", () => {
  it("plays the URL a second request mints after a 404", async () => {
    post.mockRejectedValueOnce(notFound()).mockResolvedValueOnce(token("/media/second", 45 * 60 * 1000, Date.now()));
    const { result } = renderHook(() => useVideoMediaUrl("render", "r-1", "poster"), {
      wrapper: withProviders(testQueryClient()),
    });
    await waitFor(() => expect(result.current).toBe("/media/second"), { timeout: 4000 });
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith("/video-studio/media-token", { kind: "render", id: "r-1", asset: "poster" });
  });

  it("falls back at once past a second 404 or on another failure", async () => {
    post.mockRejectedValue(notFound());
    const { result } = renderHook(() => useVideoMediaUrl("clip", "c-1", "start_frame"), {
      wrapper: withProviders(testQueryClient()),
    });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2), { timeout: 4000 });
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(post).toHaveBeenCalledTimes(2);
    expect(result.current).toBeUndefined();
  });

  it("mints the next token before the one it holds expires", async () => {
    // A token about to expire: the next one is asked within the refresh floor, not when the player loses it.
    post
      .mockResolvedValueOnce(token("/media/first", MEDIA_TOKEN_REFRESH_MARGIN_MS + 1000, Date.now()))
      .mockResolvedValueOnce(token("/media/next", 45 * 60 * 1000, Date.now()));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { result } = renderHook(() => useVideoMediaUrl("render", "r-2", "video"), {
        wrapper: withProviders(testQueryClient()),
      });
      await waitFor(() => expect(result.current).toBe("/media/first"));
      await vi.advanceTimersByTimeAsync(31 * 1000);
      await waitFor(() => expect(result.current).toBe("/media/next"));
      expect(post).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
