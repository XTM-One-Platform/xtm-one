// A video opened by its public link: one answer for a dead link, a video not rendered yet said,
// the current render played whole, and the download offered only when its managers allowed it.

import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import VideoPublic from "./VideoPublic";

const { state } = vi.hoisted(() => ({
  state: { data: undefined as unknown, isError: false, error: null as { status: number } | null },
}));

vi.mock("@/hooks/api/useVideoStudioApi", () => ({
  usePublicVideo: () => ({ data: state.data, isLoading: false, isError: state.isError, error: state.error, refetch: vi.fn() }),
  publicVideoMediaUrl: (token: string, asset: string) => `/api/v1/public/videos/${token}/media/${asset}`,
  downloadPublicVideo: vi.fn(),
}));
// The player has its own tests: here it shows what it was handed.
vi.mock("@/components/video-studio/CinemaPlayer", () => ({
  CinemaPlayer: ({ src, captions, header }: { src: string; captions?: string; header: (c: HTMLElement | null) => ReactNode }) => (
    <div data-testid="player" data-src={src} data-captions={captions ?? ""}>
      {header(null)}
    </div>
  ),
}));

const RENDERED = {
  title: "Launch film",
  rendered: true,
  download: false,
  has_poster: true,
  has_captions: true,
  captions_burned_in: false,
  duration_ms: 39_800,
  width: 1920,
  height: 1080,
  fps: 60,
  size_bytes: 12_000_000,
  rendered_at: "2026-10-20T08:00:00Z",
  chapters: [],
};

function open() {
  renderWithProviders(
    <MemoryRouter initialEntries={["/public/videos/tok"]}>
      <Routes>
        <Route path="/public/videos/:token" element={<VideoPublic />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  state.data = RENDERED;
  state.isError = false;
  state.error = null;
});

describe("VideoPublic", () => {
  it("says a link that opens nothing is gone, whatever the reason", () => {
    state.data = undefined;
    state.isError = true;
    state.error = { status: 404 };
    open();
    expect(screen.getByTestId("video.public.gone")).toHaveTextContent("This link no longer opens a video.");
    expect(screen.queryByTestId("player")).toBeNull();
  });

  it("says a video not rendered yet will play at this link once it is", () => {
    state.data = { ...RENDERED, rendered: false };
    open();
    expect(screen.getByTestId("video.public.notRendered")).toHaveTextContent("This video is not ready yet.");
    expect(screen.queryByTestId("player")).toBeNull();
  });

  it("plays the current render with its captions, and offers no download unless allowed", () => {
    open();
    const player = screen.getByTestId("player");
    expect(player.dataset.src).toBe("/api/v1/public/videos/tok/media/video");
    expect(player.dataset.captions).toBe("/api/v1/public/videos/tok/media/captions");
    expect(screen.getByRole("heading", { name: "Launch film" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download the video" })).toBeNull();
  });

  it("offers the download its managers allowed, and leaves burned-in captions out of the player", () => {
    state.data = { ...RENDERED, download: true, captions_burned_in: true };
    open();
    expect(screen.getByRole("button", { name: "Download the video" })).toBeInTheDocument();
    expect(screen.getByTestId("player").dataset.captions).toBe("");
  });
});
