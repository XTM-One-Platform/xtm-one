// The player bar on a narrow editor: Play, the time,
// the scrubber, the speed and full screen never leave the bar. The controls a key
// also reaches leave it by width tier, read here from data-leaves-below (never a
// class name, frontend/AGENTS.md). jsdom has no layout: the widths themselves are
// measured in the UX lab (303 px at 1024 with the Studio rail open).

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { VideoPreview } from "@/types";

import { Transport, type TransportState } from "./PreviewStage";
import { createPlayhead } from "./playhead";

const TRANSPORT: TransportState = { playing: false, rate: 1, loop: false, muted: false };

const preview = {
  revision: 1,
  spec: { title: "Film", scenes: [] },
  document: {
    width: 1920,
    height: 1080,
    fps: 30,
    duration: 4,
    frames: 120,
    scenes: [{ id: "s1", type: "clip", start: 0, end: 4, title: "Hello" }],
    captions: { enabled: false, cues: [] },
    preview_media: {},
    preview_media_ids: {},
  },
  duration: 4,
  narration: [],
  music: null,
  template_scenes: [],
  notes: [],
} as unknown as VideoPreview;

function mount() {
  renderWithProviders(<Transport preview={preview} playhead={createPlayhead()} transport={TRANSPORT} onTransport={() => {}} onFullscreen={() => {}} />);
}

describe("Transport on a narrow editor", () => {
  it("takes the frame steps, start and end off below 42rem: each has a key", () => {
    mount();
    for (const name of ["To the start", "Previous frame", "Next frame", "To the end"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("data-leaves-below", "2xl");
    }
  });

  it("takes loop and mute off below 36rem", () => {
    mount();
    for (const name of ["Loop", "Mute"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("data-leaves-below", "xl");
    }
  });

  it("takes the scene jumps and the duration off below 24rem, the bar at 1024 with the rail open", () => {
    mount();
    for (const name of ["Previous scene", "Next scene"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("data-leaves-below", "sm");
    }
    const duration = screen.getByTestId("video.editor.clockDuration");
    expect(duration).toHaveAttribute("data-leaves-below", "sm");
    expect(duration).toHaveTextContent("/ 0:04.0");
  });

  it("never takes Play, the time, the scrubber, the speed or full screen off", () => {
    mount();
    for (const name of ["Play", "Full screen"]) {
      expect(screen.getByRole("button", { name })).not.toHaveAttribute("data-leaves-below");
    }
    expect(screen.getByRole("slider", { name: "Position" })).not.toHaveAttribute("data-leaves-below");
    expect(screen.getByRole("combobox", { name: "Playback speed" })).not.toHaveAttribute("data-leaves-below");
    expect(screen.getByTestId("video.editor.clock")).toHaveTextContent(/^0:00\.0/);
  });
});
