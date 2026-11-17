// The stage comes through the editor's plans untouched: a plan that
// changes nothing is not loaded again, a sound keeps the URL it plays until it
// stops answering, and a source that stops answering asks for a new plan.

import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { VideoPreview, VideoPreviewMusic } from "@/types";

import { PreviewStage, type TransportState } from "./PreviewStage";
import { createPlayhead } from "./playhead";

// The frame loads no page here: the test hands it a compositor.
vi.mock("@/lib/base-path", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/base-path")>();
  return { ...actual, withBasePath: (path: string) => (path.endsWith("/preview-page") ? "about:blank" : actual.withBasePath(path)) };
});

const TRANSPORT: TransportState = { playing: false, rate: 1, loop: false, muted: false };

function plan({
  token = "1",
  music = null,
  title = "Hello",
  approximate = false,
}: { token?: string; music?: VideoPreviewMusic | null; title?: string; approximate?: boolean } = {}): VideoPreview {
  return {
    revision: 1,
    spec: { title: "Film", scenes: [] },
    document: {
      width: 1920,
      height: 1080,
      fps: 30,
      duration: 4,
      frames: 120,
      scenes: [
        { id: "s1", type: "clip", start: 0, end: 2, title, ...(approximate ? { preview_inserts_approximate: true } : {}) },
        { id: "s2", type: "clip", start: 2, end: 4 },
      ],
      captions: { enabled: false, cues: [] },
      preview_media: { "clip/s1": `/media/a?t=${token}` },
      preview_media_ids: { "clip/s1": "file-a" },
    },
    duration: 4,
    narration: [{ scene_id: "s1", start: 0, seconds: 2, text: "Hi", state: "ready", audio_url: `/voice/n?t=${token}`, key: "said-1" }],
    music,
    template_scenes: [],
    notes: [],
  };
}

const bed = (token: string, key = "bed-1"): VideoPreviewMusic => ({
  url: `/bed?t=${token}`,
  key,
  volume: 0.8,
  duck: null,
  source: "track",
  state: null,
});

function fakeCompositor() {
  let listener: (() => void) | null = null;
  return {
    load: vi.fn(() => Promise.resolve(true)),
    render: vi.fn(() => Promise.resolve(true)),
    setPlaying: vi.fn(),
    setMuted: vi.fn(),
    onSourceLost: vi.fn((next: (() => void) | null) => {
      listener = next;
    }),
    lose: () => listener?.(),
  };
}

function mount(first: VideoPreview, playhead = createPlayhead(), previewing = false) {
  const compositor = fakeCompositor();
  const onSourceLost = vi.fn();
  const stage = (preview: VideoPreview) => (
    <PreviewStage
      preview={preview}
      playhead={playhead}
      transport={TRANSPORT}
      onTransport={() => {}}
      stateLabel={() => "Being made"}
      previewing={previewing}
      stageRef={{ current: null }}
      onSourceLost={onSourceLost}
    />
  );
  const view = renderWithProviders(stage(first));
  const frame = view.container.querySelector("iframe") as HTMLIFrameElement;
  Object.defineProperty(frame, "contentWindow", { value: { __xtm: compositor } });
  fireEvent.load(frame);
  return { compositor, onSourceLost, update: (preview: VideoPreview) => view.rerender(stage(preview)) };
}

const settle = () => act(async () => {});

describe("PreviewStage across the editor's plans", () => {
  it("loads a plan that changes nothing only once", async () => {
    const { compositor, update } = mount(plan());
    await settle();
    expect(compositor.load).toHaveBeenCalledTimes(1);
    // The poll while a shot is being made: a new object, the same document.
    update(plan());
    await settle();
    expect(compositor.load).toHaveBeenCalledTimes(1);
    update(plan({ title: "Hello again" }));
    await settle();
    expect(compositor.load).toHaveBeenCalledTimes(2);
  });

  it("keeps a sound's URL across newer tokens of it, and takes the newest once it stops answering", async () => {
    const { onSourceLost, update } = mount(plan({ music: bed("1") }));
    await settle();
    const music = () => screen.getByTestId("video.editor.music");
    const voice = () => screen.getByTestId("video.editor.voice");
    update(plan({ token: "2", music: bed("2") }));
    expect(music().getAttribute("src")).toBe("/bed?t=1");
    expect(voice().getAttribute("src")).toBe("/voice/n?t=1");
    // Its token ran out: the newest URL the plans brought, and a new plan asked for.
    fireEvent.error(music());
    expect(music().getAttribute("src")).toBe("/bed?t=2");
    expect(onSourceLost).toHaveBeenCalledTimes(1);
    // Another bed (the music was changed): its own URL at once.
    update(plan({ token: "3", music: bed("3", "bed-2") }));
    expect(music().getAttribute("src")).toBe("/bed?t=3");
  });

  it("stays on a lost URL until a plan brings another one", async () => {
    const { onSourceLost, update } = mount(plan({ music: bed("1") }));
    await settle();
    const music = () => screen.getByTestId("video.editor.music");
    fireEvent.error(music());
    // The same plan again (the new one not there yet), then the fresh one.
    update(plan({ music: bed("1") }));
    expect(music().getAttribute("src")).toBe("/bed?t=1");
    update(plan({ token: "2", music: bed("2") }));
    expect(music().getAttribute("src")).toBe("/bed?t=2");
    expect(onSourceLost).toHaveBeenCalledTimes(1);
  });

  it("says the preview is approximate only on a scene whose insert it cannot draw as the render does", async () => {
    const playhead = createPlayhead();
    const { update } = mount(plan({ approximate: true }), playhead);
    await settle();
    expect(screen.getByTestId("video.editor.insertsApproximate").textContent).toContain("Preview approximate: insert compositing");
    // Past the scene, on one drawn as the render draws it: nothing said.
    act(() => playhead.set(3));
    expect(screen.queryByTestId("video.editor.insertsApproximate")).toBeNull();
    act(() => playhead.set(1));
    expect(screen.getByTestId("video.editor.insertsApproximate")).toBeTruthy();
    // Its build drawn: the next plan marks nothing, the badge goes.
    update(plan({ title: "Built" }));
    await settle();
    expect(screen.queryByTestId("video.editor.insertsApproximate")).toBeNull();
  });

  it("lays the approximate badge and the update chip out in one row, never one over the other", async () => {
    mount(plan({ approximate: true }), createPlayhead(), true);
    await settle();
    const row = screen.getByTestId("video.editor.stageChips");
    expect(within(row).getByTestId("video.editor.insertsApproximate").textContent).toContain("Preview approximate: insert compositing");
    expect(within(row).getByText("Updating the preview")).toBeTruthy();
  });

  it("asks for a new plan when the compositor loses a source", async () => {
    const { compositor, onSourceLost } = mount(plan());
    await settle();
    expect(compositor.onSourceLost).toHaveBeenCalledWith(expect.any(Function));
    compositor.lose();
    expect(onSourceLost).toHaveBeenCalledTimes(1);
  });
});
