// A timeline thumbnail shows its scene as it plays: a poster is the
// footage as it was shot - the green screen an AI shot drew - so the screen
// inserts laid on that footage are drawn over it, where the stage maps them,
// from the URL they showed while it plays the same file. The playhead
// is drawn in whole device pixels, its head centred on its line.

import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { VideoPreview, VideoPreviewInsert } from "@/types";

import { Timeline, type TimelineActions } from "./Timeline";
import { createPlayhead } from "./playhead";

const ACTIONS: TimelineActions = { split() {}, duplicate() {}, remove() {}, move() {}, reorder() {}, add() {} };

const QUAD: [number, number][] = [[0.3, 0.2], [0.7, 0.25], [0.7, 0.75], [0.3, 0.8]];

function plan(inserts: VideoPreviewInsert[] | undefined, media: Record<string, string>, ids?: Record<string, string>): VideoPreview {
  return {
    revision: 1,
    spec: { title: "Film", scenes: [] },
    document: {
      width: 1920,
      height: 1080,
      fps: 30,
      duration: 8,
      frames: 240,
      scenes: [{ id: "s1", type: "clip", start: 0, end: 8, title: "Booth", inserts }],
      captions: { enabled: false, cues: [] },
      preview_media: { "clip/s1": "/media/shot.mp4", "poster/s1": "/media/shot.jpg", ...media },
      preview_media_ids: ids,
    },
    duration: 8,
    narration: [],
    music: null,
    template_scenes: [],
    notes: [],
  };
}

function mount(preview: VideoPreview) {
  const timeline = (next: VideoPreview) => (
    <Timeline
      preview={next}
      draft={{ title: "Film", scenes: [{ id: "s1", type: "clip" }] }}
      selectedId={null}
      onSelect={() => {}}
      playhead={playhead}
      onSeek={() => {}}
      templateSceneIds={[]}
      canEdit
      canSplit={false}
      actions={ACTIONS}
    />
  );
  const playhead = createPlayhead(2);
  const view = renderWithProviders(timeline(preview));
  const poster = view.container.querySelector<HTMLImageElement>('img[src="/media/shot.jpg"]');
  if (!poster) throw new Error("the scene shows no poster");
  fireEvent.load(poster);
  return { ...view, update: (next: VideoPreview) => view.rerender(timeline(next)) };
}

const px = (value: string) => Number.parseFloat(value);

function define(proto: object, name: string, value: number) {
  Object.defineProperty(proto, name, { configurable: true, get: () => value });
}

describe("Timeline thumbnails", () => {
  beforeEach(() => {
    // happy-dom lays nothing out: the block and the poster are given the sizes a browser reads.
    define(HTMLElement.prototype, "clientWidth", 320);
    define(HTMLElement.prototype, "clientHeight", 56);
    define(HTMLImageElement.prototype, "naturalWidth", 1280);
    define(HTMLImageElement.prototype, "naturalHeight", 720);
  });

  afterEach(() => {
    for (const name of ["clientWidth", "clientHeight"]) Reflect.deleteProperty(HTMLElement.prototype, name);
    for (const name of ["naturalWidth", "naturalHeight"]) Reflect.deleteProperty(HTMLImageElement.prototype, name);
  });

  it("lays a picture inserted on the footage over the poster, on its quad", () => {
    const { container } = mount(
      plan([{ src: "insert/s1.0", quad: QUAD, fit: "cover", glow: 0, t0: 0, t1: 8, kind: "image" }], { "insert/s1.0": "/media/console.png" }),
    );
    const picture = container.querySelector<HTMLImageElement>('img[src="/media/console.png"]');
    expect(picture).not.toBeNull();
    expect(picture?.parentElement?.style.transform).toMatch(/^matrix3d\(/);
  });

  it("shows an inserted video at the frame it comes in on", () => {
    const { container } = mount(
      plan(
        [{ src: "insert/s1.0", quad: QUAD, fit: "contain", glow: 0, t0: 1, t1: 8, kind: "video", time_map: [[1, 2.5], [8, 9.5]] }],
        { "insert/s1.0": "/media/demo.mp4" },
      ),
    );
    const video = container.querySelector<HTMLVideoElement>('video[src="/media/demo.mp4"]');
    expect(video).not.toBeNull();
    fireEvent.loadedMetadata(video as HTMLVideoElement);
    expect(video?.currentTime).toBe(2.5);
  });

  it("draws nothing over the poster for a scene without inserts, or an insert whose source is not served", () => {
    for (const preview of [
      plan(undefined, {}),
      plan([{ src: "insert/s1.0", quad: QUAD, fit: "cover", glow: 0, t0: 0, t1: 8, kind: "image" }], {}),
    ]) {
      const { container, unmount } = mount(preview);
      expect(container.querySelectorAll("img")).toHaveLength(1);
      expect(container.querySelector("video")).toBeNull();
      unmount();
    }
  });

  it("keeps an inserted video's URL while the plans mint newer tokens of the same file", () => {
    const insert: VideoPreviewInsert = { src: "insert/s1.0", quad: QUAD, fit: "cover", glow: 0, t0: 0, t1: 8, kind: "video" };
    const shown = (container: HTMLElement) => container.querySelector("video")?.getAttribute("src");
    const { container, update } = mount(plan([insert], { "insert/s1.0": "/media/demo.mp4?t=1" }, { "insert/s1.0": "demo" }));
    expect(shown(container)).toBe("/media/demo.mp4?t=1");
    // The next token window: a video handed another URL would load again, blank meanwhile.
    update(plan([insert], { "insert/s1.0": "/media/demo.mp4?t=2" }, { "insert/s1.0": "demo" }));
    expect(shown(container)).toBe("/media/demo.mp4?t=1");
    // The held one stopped answering: the plan's newest.
    fireEvent.error(container.querySelector("video") as HTMLVideoElement);
    expect(shown(container)).toBe("/media/demo.mp4?t=2");
    // Another file under the key: its own URL at once.
    update(plan([insert], { "insert/s1.0": "/media/other.mp4?t=2" }, { "insert/s1.0": "other" }));
    expect(shown(container)).toBe("/media/other.mp4?t=2");
  });
});

describe("Timeline toolbar", () => {
  it("names the Add a scene button, and says its name on focus when it is folded to its icon", async () => {
    mount(plan(undefined, {}));
    const add = screen.getByRole("button", { name: "Add a scene" });
    fireEvent.focus(add);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Add a scene");
  });
});

describe("Timeline playhead", () => {
  afterEach(() => {
    Reflect.deleteProperty(window, "devicePixelRatio");
  });

  it.each([1, 1.25, 1.5, 2])("draws its line and its head in whole device pixels, the head centred on the line, at %sx", (ratio) => {
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: ratio });
    mount(plan(undefined, {}));
    const line = screen.getByTestId("video.editor.playhead");
    const head = line.firstElementChild as HTMLElement;
    const devicePixels = (value: string) => px(value) * ratio;
    const width = devicePixels(line.style.width);
    // One device pixel below a 2x screen, two from one: never a width the browser draws one or two pixels wide by turns.
    expect(width).toBeCloseTo(ratio >= 2 ? 2 : 1, 4);
    for (const side of [head.style.borderLeftWidth, head.style.borderRightWidth, head.style.borderTopWidth]) {
      expect(Math.abs(devicePixels(side) - Math.round(devicePixels(side)))).toBeLessThan(1e-4);
    }
    // The head is the line's width with a side on each hand: its middle is the line's middle.
    expect(devicePixels(head.style.width)).toBeCloseTo(width, 4);
    expect(devicePixels(head.style.left)).toBeCloseTo(-devicePixels(head.style.borderLeftWidth), 4);
    expect(head.style.borderLeftWidth).toBe(head.style.borderRightWidth);
  });
});
