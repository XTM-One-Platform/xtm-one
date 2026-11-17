// An AI shot's scene shows what the shot opened on: the frame kept
// from the recording, shot or video file it started from, or its start image,
// with where it was taken; a shot that started from nothing shows none.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { VideoClip } from "@/types";
import { SceneInspector } from "./SceneInspector";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const SHOT: VideoClip = {
  id: "c-1",
  user_id: "u-1",
  name: "The dashboard comes alive",
  provider: "gemini",
  model: "veo-3.1",
  prompt: "The dashboard comes alive",
  params: {},
  status: "ready",
  progress: 100,
  error: null,
  duration_ms: 8000,
  width: 1920,
  height: 1080,
  has_audio: false,
  file_id: "f-video",
  poster_file_id: "f-poster",
  start_frame_file_id: null,
  created_at: null,
  finished_at: null,
  keep: false,
  last_used_at: null,
  expired_at: null,
  expires_at: null,
};

let shot: VideoClip = SHOT;

function inspect() {
  renderWithProviders(
    <SceneInspector
      scene={{ id: "s1", type: "clip", clip_id: "c-1" }}
      planned={undefined}
      position={1}
      fromTemplate={false}
      canEdit
      locked={undefined}
      getLocalTime={() => 0}
      speechSpeed={1}
      onChange={() => undefined}
    />,
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => Promise.resolve(path === "/video-studio/clips/c-1" ? shot : {})) as typeof api.get);
  vi.mocked(api.post).mockImplementation(((path: string, body: { asset?: string } | undefined) =>
    Promise.resolve(path === "/video-studio/media-token" ? { url: `/media/${body?.asset ?? ""}` } : {})) as typeof api.post);
});

describe("SceneInspector's first frame of an AI shot", () => {
  it("shows the frame cut from a recording's step, and where it was taken", async () => {
    shot = {
      ...SHOT,
      start_frame_file_id: "f-frame",
      params: { reference_video: { kind: "recording", id: "t-1", file_id: "f-take", name: "OpenCRQ demo", at_seconds: 4.5, step: 2, frame_file_id: "f-frame" } },
    };
    inspect();
    const frame = await screen.findByTestId("video.editor.shot-start-frame");
    expect(frame).toHaveAttribute("src", "/media/start_frame");
    expect(frame).toHaveAttribute("alt", "The frame the shot opened on");
    expect(screen.getByText("After step 2 (4.5 s) of OpenCRQ demo.")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledWith("/video-studio/media-token", { kind: "clip", id: "c-1", asset: "start_frame" });
  });

  it("says a moment, or the last frame a shot continues from", async () => {
    shot = {
      ...SHOT,
      start_frame_file_id: "f-frame",
      params: { reference_video: { kind: "shot", id: "c-0", file_id: "f-0", name: "Globe", at_seconds: null, frame_file_id: "f-frame" } },
    };
    inspect();
    expect(await screen.findByText("The last frame of Globe.")).toBeInTheDocument();
  });

  it("says a frame was cropped to the shot's shape", async () => {
    shot = {
      ...SHOT,
      start_frame_file_id: "f-frame",
      params: { reference_video: { kind: "file", id: "f-v", file_id: "f-v", name: "keynote.mp4", at_seconds: 12, crop: "center", frame_file_id: "f-frame" } },
    };
    inspect();
    expect(await screen.findByText("At 12.0 s of keynote.mp4. Its middle, cut to the shot's shape.")).toBeInTheDocument();
  });

  it("shows the start image of a shot made from a picture", async () => {
    shot = { ...SHOT, start_frame_file_id: "f-image", params: { reference_file_id: "f-image" } };
    inspect();
    expect(await screen.findByText("The image the shot started from.")).toBeInTheDocument();
  });

  it("shows nothing for a shot that started from its prompt alone", async () => {
    shot = SHOT;
    inspect();
    // The shot is read: its name replaces the placeholder.
    await screen.findByText("The dashboard comes alive");
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/video-studio/clips/c-1"));
    expect(screen.queryByTestId("video.editor.shot-start-frame")).toBeNull();
    expect(screen.queryByText("First frame")).toBeNull();
    expect(vi.mocked(api.post).mock.calls.some(([path]) => path === "/video-studio/media-token")).toBe(false);
  });
});

// A made shot keeps its own first, middle and last frames, with what they measure.
describe("SceneInspector's frames of a made AI shot", () => {
  it("shows the three frames and how they measure against the start frame", async () => {
    shot = {
      ...SHOT,
      first_frame_file_id: "f-first",
      mid_frame_file_id: "f-mid",
      last_frame_file_id: "f-last",
      frame_qc: { reference_ssim: 0.83, reference_text_match: 0.5, motion_ssim: 0.41 },
    };
    inspect();
    expect(await screen.findByTestId("video.editor.shot-first_frame")).toHaveAttribute("src", "/media/first_frame");
    expect(await screen.findByTestId("video.editor.shot-mid_frame")).toHaveAttribute("src", "/media/mid_frame");
    expect(await screen.findByTestId("video.editor.shot-last_frame")).toHaveAttribute("src", "/media/last_frame");
    expect(screen.getByText("First frame vs start frame: 0.83; Words still read: 50%; Last frame vs first: 0.41")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledWith("/video-studio/media-token", { kind: "clip", id: "c-1", asset: "mid_frame" });
  });

  it("shows no frames for a shot that kept none", async () => {
    shot = SHOT;
    inspect();
    await screen.findByText("The dashboard comes alive");
    expect(screen.queryByText("The shot's frames")).toBeNull();
  });

  // A drifting camera locked off, and the fallback model that made the take kept.
  it("says when its camera was stabilised and which fallback model made it", async () => {
    shot = {
      ...SHOT,
      model: "kling-v3",
      first_frame_file_id: "f-first",
      mid_frame_file_id: "f-mid",
      last_frame_file_id: "f-last",
      frame_qc: { motion_ssim: 0.97 },
      stabilisation: { applied: true, raw_motion: 0.42, residual_motion: 0.03, unstabilised_kept: true },
      fallback: { models: ["kling-v3"], tried: [{ model: "veo-3.1", provider: "gemini", reason: "camera moved" }], made_by: "kling-v3", fell_back: true },
    };
    inspect();
    expect(await screen.findByText("Last frame vs first: 0.97; Stabilised: motion 0.42 to 0.03; Made by the fallback model kling-v3")).toBeInTheDocument();
  });
});

// A screen insert follows its surface through the footage when asked.
describe("SceneInspector's screen insert", () => {
  it("turns following the camera on and off", async () => {
    const changed = vi.fn();
    const insert = { type: "screen_insert", source: { clip_id: "c-2" }, quad: [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]] };
    renderWithProviders(
      <SceneInspector
        scene={{ id: "s1", type: "clip", clip_id: "c-1", overlays: [insert] }}
        planned={undefined}
        position={1}
        fromTemplate={false}
        canEdit
        locked={undefined}
        getLocalTime={() => 0}
        speechSpeed={1}
        onChange={changed}
      />,
    );
    const follow = await screen.findByRole("switch", { name: "Follow the camera" });
    expect(follow).not.toBeChecked();
    follow.click();
    await waitFor(() => expect(changed).toHaveBeenCalledWith({ overlays: [{ ...insert, track: true }] }, "s1.overlays.0"));
  });

  // A quad left to the render is said, and cannot follow the camera.
  it("says an auto quad is found on the footage, and keeps it off the camera", async () => {
    const insert = { type: "screen_insert", source: { clip_id: "c-2" }, quad: "auto" };
    renderWithProviders(
      <SceneInspector
        scene={{ id: "s1", type: "clip", clip_id: "c-1", overlays: [insert] }}
        planned={undefined}
        position={1}
        fromTemplate={false}
        canEdit
        locked={undefined}
        getLocalTime={() => 0}
        speechSpeed={1}
        onChange={vi.fn()}
      />,
    );
    const box = await screen.findByTestId("video.editor.screen-insert");
    expect(box).toHaveTextContent("Its corners are the bezel the render finds on the footage's first frame (quad auto).");
    expect(screen.getByRole("switch", { name: "Follow the camera" })).toBeDisabled();
  });

  // A wall of screens is one insert, one source per pane.
  it("says how many sources a wall of panes maps", async () => {
    const wall = {
      type: "screen_insert",
      panes: 3,
      sources: [{ clip_id: "c-2" }, { take_id: "t-1" }, { file_id: "f-1" }],
      quad: [[0.1, 0.2], [0.9, 0.2], [0.9, 0.5], [0.1, 0.5]],
    };
    renderWithProviders(
      <SceneInspector
        scene={{ id: "s1", type: "clip", clip_id: "c-1", overlays: [wall] }}
        planned={undefined}
        position={1}
        fromTemplate={false}
        canEdit
        locked={undefined}
        getLocalTime={() => 0}
        speechSpeed={1}
        onChange={vi.fn()}
      />,
    );
    expect(
      await screen.findByText("Maps 3 sources (one per pane) onto a surface of the footage, frame by frame: the assistant sets its corners."),
    ).toBeInTheDocument();
  });

  // A keyed insert: its key colour, its tolerance and feather; a brand mark named as what it maps.
  it("shows its key and what it maps, and sets the key's tolerance", async () => {
    const changed = vi.fn();
    const insert = {
      type: "screen_insert",
      source: { asset: "logo_mark" },
      quad: [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]],
      key: { color: "green" },
    };
    renderWithProviders(
      <SceneInspector
        scene={{ id: "s1", type: "clip", clip_id: "c-1", overlays: [insert] }}
        planned={undefined}
        position={1}
        fromTemplate={false}
        canEdit
        locked={undefined}
        getLocalTime={() => 0}
        speechSpeed={1}
        onChange={changed}
      />,
    );
    const box = await screen.findByTestId("video.editor.screen-insert");
    expect(box).toHaveTextContent("Maps the brand's mark onto a surface of the footage");
    expect(screen.getByRole("combobox", { name: "Chroma key" })).toHaveTextContent("Green screen");
    const tolerance = screen.getAllByRole("spinbutton", { name: "Tolerance" })[0];
    fireEvent.change(tolerance, { target: { value: "45" } });
    await waitFor(() =>
      expect(changed).toHaveBeenCalledWith({ overlays: [{ ...insert, key: { color: "green", tolerance: 0.45 } }] }, "s1.overlays.0"),
    );
  });
});

// A ready shot generated again keeps its take; a shot queued on a cooling provider waits.
describe("SceneInspector's state of an AI shot", () => {
  it("says a regeneration failed while the previous take still plays, with the reason", async () => {
    shot = {
      ...SHOT,
      regeneration: { state: "failed", reason: "content drift: the boat turned into a leaf", attempts_failed: 1, previous_take_plays: true },
    };
    inspect();
    const box = await screen.findByTestId("video.editor.shot-regeneration-failed");
    expect(box).toHaveTextContent("The last regeneration failed: the previous take still plays");
    expect(box).toHaveTextContent("content drift: the boat turned into a leaf");
    expect(box).toHaveTextContent("The previous take keeps playing until a new one is ready.");
    expect(screen.getByRole("button", { name: "Generate again" })).toBeEnabled();
    expect(screen.queryByTestId("video.editor.shot-failed")).toBeNull();
  });

  it("says a shot waits in the queue for its provider until a time, never that it failed", async () => {
    shot = { ...SHOT, status: "pending", progress: 0, waiting_for_provider_until: "2026-10-20T09:45:00Z" };
    inspect();
    const line = await screen.findByTestId("video.editor.shot-generating");
    expect(line).toHaveTextContent(/^Waiting in the queue until .+: its provider refused for its quota or rate limit\. It starts by itself then\.$/);
    expect(screen.queryByTestId("video.editor.shot-failed")).toBeNull();
    expect(screen.queryByTestId("video.editor.shot-regeneration-failed")).toBeNull();
  });
});

// A closing card carries a kicker like an opening one.
describe("SceneInspector's closing card", () => {
  it("edits the kicker above its title", async () => {
    renderWithProviders(
      <SceneInspector
        scene={{ id: "end", type: "outro", title: "See you there", kicker: "Keynote" }}
        planned={undefined}
        position={2}
        fromTemplate={false}
        canEdit
        locked={undefined}
        getLocalTime={() => 0}
        speechSpeed={1}
        onChange={() => undefined}
      />,
    );
    expect(await screen.findByLabelText("Kicker")).toHaveValue("Keynote");
  });
});
