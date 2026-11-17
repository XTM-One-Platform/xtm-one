// The Video Studio player: its own controls over the `<video>`, the
// keys of a video player while the focus is in it, the browser's shortcuts
// and a focused control's own keys left alone, and the viewport fullscreen
// left on `Esc` before anything around the player closes.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CinemaPlayer } from "./CinemaPlayer";

// The player remembers the volume across videos: each test starts from none.
beforeEach(() => localStorage.clear());

function open(props: Partial<React.ComponentProps<typeof CinemaPlayer>> = {}) {
  renderWithProviders(<CinemaPlayer src="/media/video" title="Brand film" durationHint={64} videoTestId="player.video" {...props} />);
  const player = screen.getByRole("region", { name: "Brand film" });
  const video = screen.getByTestId("player.video") as HTMLVideoElement;
  return { player, video };
}

describe("CinemaPlayer", () => {
  it("draws its own controls rather than the browser's", () => {
    const { video } = open();
    expect(video).not.toHaveAttribute("controls");
    const controls = screen.getByTestId("video.player.controls");
    expect(within(controls).getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(within(controls).getByRole("button", { name: "Back 10 seconds" })).toBeInTheDocument();
    expect(within(controls).getByRole("button", { name: "Mute" })).toBeInTheDocument();
    expect(within(controls).getByRole("button", { name: "Settings" })).toBeInTheDocument();
    expect(within(controls).getByRole("slider", { name: "Seek" })).toHaveAttribute("aria-valuetext", "0:00 of 1:04");
    // No captions file, no captions switch.
    expect(screen.queryByRole("button", { name: /captions/ })).toBeNull();
    // Paused, the picture offers to play.
    expect(screen.getByTestId("video.player.bigPlay")).toBeInTheDocument();
  });

  it("keeps the focus in the player when its big Play button goes away", () => {
    const { player, video } = open();
    fireEvent.click(screen.getByTestId("video.player.bigPlay"));
    expect(video.paused).toBe(false);
    expect(screen.queryByTestId("video.player.bigPlay")).toBeNull();
    expect(player).toHaveFocus();
    fireEvent.keyDown(document.activeElement ?? player, { key: "k" });
    expect(video.paused).toBe(true);
  });

  it("answers a video player's keys while the focus is in it", () => {
    const { player, video } = open();
    player.focus();

    expect(fireEvent.keyDown(player, { key: " " })).toBe(false);
    expect(video.paused).toBe(false);
    expect(screen.queryByTestId("video.player.bigPlay")).toBeNull();
    expect(screen.getByTestId("video.player.controls")).toContainElement(screen.getByRole("button", { name: "Pause" }));

    fireEvent.keyDown(player, { key: "ArrowRight" });
    expect(video.currentTime).toBe(5);
    fireEvent.keyDown(player, { key: "l" });
    expect(video.currentTime).toBe(15);
    fireEvent.keyDown(player, { key: "j" });
    expect(video.currentTime).toBe(5);
    fireEvent.keyDown(player, { key: "End" });
    expect(video.currentTime).toBe(64);
    fireEvent.keyDown(player, { key: "5" });
    expect(video.currentTime).toBe(32);

    fireEvent.keyDown(player, { key: "m" });
    expect(video.muted).toBe(true);
    expect(screen.getByRole("button", { name: "Unmute" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("xtm-video-player-volume") ?? "{}")).toEqual({ volume: 1, muted: true });
    fireEvent.keyDown(player, { key: ">" });
    expect(video.playbackRate).toBe(1.25);

    fireEvent.keyDown(player, { key: "k" });
    expect(video.paused).toBe(true);
  });

  it("leaves the browser's shortcuts and a focused control's own keys alone", () => {
    const { player, video } = open();
    expect(fireEvent.keyDown(player, { key: "f", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(player, { key: "ArrowLeft", altKey: true })).toBe(true);
    // Space on a button presses that button; it does not also toggle the video.
    const mute = screen.getByRole("button", { name: "Mute" });
    expect(fireEvent.keyDown(mute, { key: " " })).toBe(true);
    expect(video.paused).toBe(true);
  });

  it("switches the captions when the video has a file for them", () => {
    const { player } = open({ captions: "/media/captions" });
    const on = screen.getByRole("button", { name: "Hide the captions" });
    expect(on).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(player, { key: "c" });
    expect(screen.getByRole("button", { name: "Show the captions" })).toHaveAttribute("aria-pressed", "false");
  });

  it("leaves its viewport fullscreen on Esc, and says it handled the key", () => {
    const { player } = open();
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(player).toHaveAttribute("data-deck-viewport");
    expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeInTheDocument();

    expect(fireEvent.keyDown(player, { key: "Escape" })).toBe(false);
    expect(player).not.toHaveAttribute("data-deck-viewport");
    // Not fullscreen any more: the next Esc is for whatever holds the player.
    expect(fireEvent.keyDown(player, { key: "Escape" })).toBe(true);
  });

  it("offers no fullscreen of its own when it already fills a window the browser will not take fullscreen", () => {
    open({ layout: "fill", fillsWindow: true });
    expect(screen.queryByRole("button", { name: "Full screen" })).toBeNull();
  });

  it("waits for its media URL without controls to press", () => {
    open({ src: undefined });
    expect(screen.getByText("Loading the video")).toBeInTheDocument();
    expect(screen.queryByTestId("video.player.controls")).toBeNull();
  });

  it("shows an autoplaying video loading, never a Play button it is about to take away", async () => {
    const { video } = open({ autoPlay: true });
    expect(screen.getByText("Loading the video")).toBeInTheDocument();
    expect(screen.queryByTestId("video.player.bigPlay")).toBeNull();
    await act(async () => {
      fireEvent.loadedMetadata(video);
    });
    expect(video.paused).toBe(false);
    expect(screen.queryByTestId("video.player.bigPlay")).toBeNull();
    expect(screen.queryByText("Loading the video")).toBeNull();
  });

  it("offers Play once the browser refuses to start an autoplaying video", async () => {
    const { video } = open({ autoPlay: true });
    vi.spyOn(video, "play").mockRejectedValue(new DOMException("Autoplay refused", "NotAllowedError"));
    await act(async () => {
      fireEvent.loadedMetadata(video);
    });
    expect(screen.getByTestId("video.player.bigPlay")).toBeInTheDocument();
    expect(screen.queryByText("Loading the video")).toBeNull();
  });

  it("marks a render's chapters, names the one playing and seeks to one from its menu", () => {
    const { video } = open({
      chapters: [
        { start: 0, type: "title", title: "From Normandy" },
        { start: 20, type: "clip", title: null },
        { start: 41.5, type: "outro", title: "Thanks" },
      ],
    });
    expect(screen.getAllByTestId("video.player.chapterMark")).toHaveLength(3);
    expect(screen.getByRole("slider", { name: "Seek" })).toHaveAttribute("aria-valuetext", "0:00 of 1:04 - From Normandy");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Chapters" }), { button: 0, ctrlKey: false });
    const menu = screen.getByRole("menu");
    // A chapter with no name of its own is named by its place and its scene's type.
    fireEvent.click(within(menu).getByRole("menuitem", { name: /2\. Scene 2 - AI shot/ }));
    expect(video.currentTime).toBe(20);
    fireEvent.timeUpdate(video);
    expect(screen.getByTestId("video.player.chapter")).toHaveTextContent("Scene 2 - AI shot");
  });

  it("draws no chapter marks for a video with one chapter or none", () => {
    open({ chapters: [{ start: 0, type: "title", title: "Only one" }] });
    expect(screen.queryAllByTestId("video.player.chapterMark")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Chapters" })).toBeNull();
  });

  it("picks the video up where the viewer left it, and offers to start over", () => {
    localStorage.setItem("xtm-video-player-resume", JSON.stringify({ "render:r-1": { t: 30, at: Date.now() } }));
    const { video } = open({ resumeKey: "render:r-1" });
    Object.defineProperty(video, "duration", { configurable: true, value: 64 });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(30);
    expect(screen.getByTestId("video.player.resumed")).toHaveTextContent("Resumed at 0:30");
    fireEvent.click(screen.getByRole("button", { name: "Start over" }));
    expect(video.currentTime).toBe(0);
    expect(screen.queryByTestId("video.player.resumed")).toBeNull();
    // Where they stop is kept for next time; the end of the video forgets it.
    video.currentTime = 42;
    fireEvent.pause(video);
    expect(JSON.parse(localStorage.getItem("xtm-video-player-resume") ?? "{}")["render:r-1"].t).toBe(42);
    fireEvent.ended(video);
    expect(JSON.parse(localStorage.getItem("xtm-video-player-resume") ?? "{}")["render:r-1"]).toBeUndefined();
  });

  it("asks for a new URL when the one playing stops answering, and goes on where it was", () => {
    const lost = vi.fn();
    const { video, rerender } = openWithRerender({ onSourceLost: lost });
    video.currentTime = 25;
    Object.defineProperty(video, "error", { configurable: true, value: { code: 2 } });
    fireEvent.error(video);
    expect(lost).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Loading the video")).toBeInTheDocument();
    rerender({ src: "/media/video?token=new", onSourceLost: lost });
    video.currentTime = 0;
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(25);
    expect(screen.queryByTestId("video.player.failed")).toBeNull();
    // Failing again at once is not a token past its life: the player says so and offers to try again.
    fireEvent.error(video);
    expect(lost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent("The video could not be played.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(lost).toHaveBeenCalledTimes(2);
  });

  it("says a video it cannot play failed, and loads it again on Try again", () => {
    const { video } = open();
    const load = vi.spyOn(video, "load").mockImplementation(() => undefined);
    Object.defineProperty(video, "error", { configurable: true, value: { code: 4 } });
    fireEvent.error(video);
    expect(screen.getByTestId("video.player.failed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(load).toHaveBeenCalled();
    expect(screen.queryByTestId("video.player.failed")).toBeNull();
  });

  it("shows and says what a key did", () => {
    const { player } = open();
    fireEvent.keyDown(player, { key: "ArrowDown" });
    expect(screen.getByRole("status")).toHaveTextContent("Volume 90%");
    expect(screen.getByTestId("video.player.flash")).toHaveTextContent("Volume 90%");
    fireEvent.keyDown(player, { key: "l" });
    expect(screen.getByRole("status")).toHaveTextContent("10 seconds forward");
    fireEvent.keyDown(player, { key: ">" });
    expect(screen.getByRole("status")).toHaveTextContent("Speed 1.25x");
  });

  it("steps frame by frame, paused, at the video's frame rate", () => {
    const { player, video } = open({ fps: 25 });
    fireEvent.click(screen.getByTestId("video.player.bigPlay"));
    video.currentTime = 10;
    fireEvent.keyDown(player, { key: "." });
    expect(video.paused).toBe(true);
    expect(video.currentTime).toBeCloseTo(10.04, 5);
    fireEvent.keyDown(player, { key: "," });
    expect(video.currentTime).toBeCloseTo(10, 5);
  });

  it("opens its key sheet with ?, whose own keys never drive the video", () => {
    const { player, video } = open();
    fireEvent.keyDown(player, { key: "?" });
    const sheet = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(sheet).getByText("Previous or next frame, paused")).toBeInTheDocument();
    fireEvent.keyDown(sheet, { key: "k" });
    expect(video.paused).toBe(true);
  });

  it("loops the video from its settings", () => {
    const { video } = open();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Settings" }), { button: 0, ctrlKey: false });
    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Loop" }));
    expect(video.loop).toBe(true);
  });

  it("skips back or forward on a double tap on a side of a touch screen", () => {
    const { video } = open();
    const surface = screen.getByTestId("video.player.surface");
    vi.spyOn(surface, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 300, height: 170, right: 300, bottom: 170, x: 0, y: 0, toJSON: () => ({}) });
    video.currentTime = 30;
    fireEvent.pointerDown(surface, { pointerType: "touch" });
    fireEvent.click(surface, { clientX: 280 });
    fireEvent.click(surface, { clientX: 280 });
    expect(video.currentTime).toBe(40);
    fireEvent.click(surface, { clientX: 20 });
    fireEvent.click(surface, { clientX: 20 });
    expect(video.currentTime).toBe(30);
    // Two taps apart, never a skip: each one waits to be sure it is alone.
    expect(video.paused).toBe(true);
  });
});

function openWithRerender(props: Partial<React.ComponentProps<typeof CinemaPlayer>> = {}) {
  const base = { src: "/media/video", title: "Brand film", durationHint: 64, videoTestId: "player.video" };
  const view = renderWithProviders(<CinemaPlayer {...base} {...props} />);
  const video = screen.getByTestId("player.video") as HTMLVideoElement;
  return {
    video,
    rerender: (next: Partial<React.ComponentProps<typeof CinemaPlayer>>) => view.rerender(<CinemaPlayer {...base} {...next} />),
  };
}
