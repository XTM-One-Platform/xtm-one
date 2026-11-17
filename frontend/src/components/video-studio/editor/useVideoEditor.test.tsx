// The editor's plans: only an edit says "Updating the preview"; a poll
// or a lost source plans quietly, a quiet preview is planned again before its
// URLs' tokens run out, and a hidden page waits until it is seen.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import type { VideoPreview, VideoProject } from "@/types";

import { useVideoEditor } from "./useVideoEditor";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}));

const PROJECT = {
  id: "v-1",
  revision: 1,
  can_manage: true,
  spec: { title: "Film", scenes: [{ id: "s1", type: "title", title: "Hello" }] },
} as unknown as VideoProject;

const PLAN = {
  revision: 1,
  spec: { title: "Film", scenes: [] },
  document: { width: 1920, height: 1080, fps: 30, duration: 4, frames: 120, scenes: [], captions: { enabled: false, cues: [] } },
  duration: 4,
  narration: [],
  music: null,
  template_scenes: [],
  notes: [],
} as VideoPreview;

const planned = () => vi.mocked(api.post).mock.calls.filter(([path]) => path === "/video-studio/projects/v-1/preview").length;

let answers: ((plan: VideoPreview) => void)[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  answers = [];
  vi.mocked(api.post).mockReset();
  vi.mocked(api.put).mockReset();
  vi.mocked(api.post).mockImplementation((() => new Promise<VideoPreview>((resolve) => answers.push(resolve))) as typeof api.post);
  vi.mocked(api.put).mockImplementation((() => new Promise(() => {})) as typeof api.put);
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

function mount() {
  return renderHook(() => useVideoEditor(PROJECT), { wrapper: withProviders(testQueryClient()) });
}

async function answer() {
  await act(async () => {
    answers.shift()?.(PLAN);
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("useVideoEditor's plans", () => {
  it("says the preview is being updated for an edit only", async () => {
    const { result } = mount();
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(1);
    expect(result.current.previewing).toBe(true);
    await answer();
    expect(result.current.previewing).toBe(false);
    // A source lost, a shot polled: the same draft planned again, quietly.
    act(() => result.current.refreshPreview());
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(2);
    expect(result.current.previewing).toBe(false);
    await answer();
    // An edit: the stage says it is catching up.
    act(() => result.current.edit((spec) => ({ ...spec, title: "Another film" })));
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(3);
    expect(result.current.previewing).toBe(true);
  });

  it("plans a quiet preview again before its tokens run out, and not while the page is hidden", async () => {
    mount();
    await act(() => vi.advanceTimersByTimeAsync(300));
    await answer();
    expect(planned()).toBe(1);
    // The tick lands, then the plan it asks for once the draft has settled.
    await act(() => vi.advanceTimersByTimeAsync(10 * 60 * 1000));
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(2);
    await answer();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    await act(() => vi.advanceTimersByTimeAsync(10 * 60 * 1000));
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(2);
    // Seen again: the plan it missed, at once.
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(planned()).toBe(3);
  });
});
