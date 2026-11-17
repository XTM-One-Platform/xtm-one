// A player on confined media URLs: it keeps the URL it started with
// while that one answers - a token the query refreshed meanwhile never
// restarts the video - and takes a new one once the one playing stopped
// answering. Where the viewer left it is remembered per render.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { VideoPlayer } from "./VideoPlayer";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

let minted = 0;

beforeEach(() => {
  minted = 0;
  localStorage.clear();
  vi.mocked(api.post).mockImplementation(async (_path: string, body?: unknown) => {
    minted += 1;
    const { asset } = body as { asset: string };
    return { url: `/media/${asset}?token=${minted}` };
  });
});

describe("VideoPlayer", () => {
  it("keeps the URL it plays while it answers, and takes a new one once it stopped", async () => {
    const { client } = renderWithProviders(<VideoPlayer kind="render" id="r-1" title="Brand film" durationHint={64} videoTestId="player.video" />);
    const video = screen.getByTestId("player.video") as HTMLVideoElement;
    await waitFor(() => expect(video.getAttribute("src")).toMatch(/^\/media\/video\?token=\d+$/));
    const first = video.getAttribute("src");

    // The query mints a fresh token (a refocused window, a stale entry): the video plays on.
    await client.invalidateQueries({ queryKey: ["video-studio", "media"] });
    await waitFor(() => expect(minted).toBeGreaterThan(2));
    expect(video.getAttribute("src")).toBe(first);

    // The one playing stops answering: the player takes the new one.
    Object.defineProperty(video, "error", { configurable: true, value: { code: 4 } });
    fireEvent.error(video);
    await waitFor(() => expect(video.getAttribute("src")).not.toBe(first));
    expect(video.getAttribute("src")).toMatch(/^\/media\/video\?token=\d+$/);
  });

  it("remembers where the viewer left a render under the render", async () => {
    renderWithProviders(<VideoPlayer kind="render" id="r-1" title="Brand film" durationHint={64} videoTestId="player.video" />);
    const video = screen.getByTestId("player.video") as HTMLVideoElement;
    await waitFor(() => expect(video.getAttribute("src")).toBeTruthy());
    Object.defineProperty(video, "duration", { configurable: true, value: 64 });
    video.currentTime = 33;
    fireEvent.pause(video);
    expect(JSON.parse(localStorage.getItem("xtm-video-player-resume") ?? "{}")).toHaveProperty(["render:r-1", "t"], 33);
  });
});
