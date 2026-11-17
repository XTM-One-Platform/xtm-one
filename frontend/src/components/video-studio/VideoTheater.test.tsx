// The theater: Play opens the video over the whole window as a modal
// of its own - the page behind out of reach, the video playing at once, the
// render's downloads on top - and Esc or Close hands the focus back to what
// opened it. A player's own Esc (leaving its fullscreen) does not close it.

import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { VideoRender } from "@/types";
import { VideoTheater, type TheaterMedia } from "./VideoTheater";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const RENDER = {
  id: "r-1",
  project_id: "v-1",
  revision: 4,
  quality: "high",
  status: "ready",
  size_bytes: 2_000_000,
  file_id: "f-1",
  width: 1920,
  height: 1080,
  fps: 60,
  duration_ms: 64_000,
  captions_file_id: null,
  poster_file_id: "p-1",
} as unknown as VideoRender;

function Opener({ media }: { media: TheaterMedia }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      {open && <VideoTheater media={media} title="Launch film" subtitle="High (1080p, 60 fps) - revision 4" onClose={() => setOpen(false)} />}
    </>
  );
}

function openTheater(media: TheaterMedia = { kind: "render", render: RENDER }) {
  renderWithProviders(<Opener media={media} />);
  const opener = screen.getByRole("button", { name: "Open" });
  opener.focus();
  fireEvent.click(opener);
  return { opener, theater: screen.getByRole("dialog", { name: "Launch film" }) };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.post).mockImplementation(((path: string, body: { kind?: string; asset?: string } | undefined) =>
    Promise.resolve(path === "/video-studio/media-token" ? { url: `/media/${body?.kind}/${body?.asset}` } : {})) as typeof api.post);
});

describe("VideoTheater", () => {
  it("plays the render over the page, which is out of reach meanwhile", async () => {
    const { opener, theater } = openTheater();

    expect(theater).toHaveAttribute("aria-modal", "true");
    expect(opener.closest("[inert]")).not.toBeNull();
    expect(theater.closest("[inert]")).toBeNull();
    expect(within(theater).getByText("High (1080p, 60 fps) - revision 4")).toBeInTheDocument();
    const video = within(theater).getByTestId("video.theater.player");
    await waitFor(() => expect(video).toHaveAttribute("src", "/media/render/video"));
    expect(video).toHaveAttribute("autoplay");
    // The player has the focus, so its keys answer at once.
    expect(within(theater).getByRole("region", { name: "Launch film" })).toHaveFocus();
    expect(within(theater).getByRole("button", { name: "Download the video" })).toBeInTheDocument();
  });

  it("closes on Esc and hands the focus back to what opened it", () => {
    const { opener, theater } = openTheater();
    expect(fireEvent.keyDown(within(theater).getByRole("region", { name: "Launch film" }), { key: "Escape" })).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener.closest("[inert]")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("closes from its Close button", () => {
    const { theater } = openTheater();
    fireEvent.click(within(theater).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("stays open on the Esc a player in fullscreen keeps for itself", () => {
    const { theater } = openTheater();
    const player = within(theater).getByRole("region", { name: "Launch film" });
    // The theater already fills the window: only a fullscreen the browser grants
    // is offered, and Esc in it is the player's. Here that is the viewport mode.
    player.setAttribute("data-deck-viewport", "");
    fireEvent.keyDown(player, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Launch film" })).toBeInTheDocument();
  });

  it("plays a recording without a render's downloads", async () => {
    const { theater } = openTheater({ kind: "take", id: "t-1" });
    await waitFor(() => expect(within(theater).getByTestId("video.theater.player")).toHaveAttribute("src", "/media/take/video"));
    expect(within(theater).queryByRole("button", { name: "Download the video" })).toBeNull();
  });
});
