// A video shared read-only opens the way a read-only deck does:
// no inspector, no quality check, no undo, no editing tool on the timeline -
// not drawn rather than disabled. Once rendered, a reader watches the render,
// like its public link. An editor keeps every control.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { VideoPreview, VideoProject, VideoQualityReport, VideoRender } from "@/types";
import VideoEditor from "./VideoEditor";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
// The compositor draws on a canvas, the rail is a chat and the inspector's
// tabs have their own data: this test is about which of them are drawn.
vi.mock("@/components/video-studio/editor/PreviewStage", () => ({ PreviewStage: () => null, Transport: () => null }));
vi.mock("@/components/video-studio/VideoStudioRail", () => ({ VideoStudioRail: () => null, VideoStudioHandle: () => null }));
vi.mock("@/components/video-studio/editor/SceneInspector", () => ({ SceneInspector: () => <div data-testid="scene-inspector" /> }));
vi.mock("@/components/video-studio/editor/VideoSettings", () => ({ VideoSettings: () => null }));
vi.mock("@/components/video-studio/editor/MediaPanel", () => ({ MediaPanel: () => null }));
vi.mock("@/components/video-studio/editor/RendersPanel", () => ({ RendersPanel: () => <div data-testid="renders-panel" /> }));
vi.mock("@/components/video-studio/VideoShareDialog", () => ({ VideoShareDialog: () => null }));

/** The header's Render verb: it opens the Renders tab, its accessible name says so. */
const RENDER_VERB = "Render: open the Renders tab to pick the quality and start the render";
const SPEC = { title: "Brand film", scenes: [{ id: "s1", type: "statement", text: "Hello" }] };
const PREVIEW = {
  revision: 3,
  spec: SPEC,
  document: { scenes: [], duration: 4, fps: 30 },
  duration: 4,
  narration: [],
  music: null,
  template_scenes: [],
  notes: [],
} as unknown as VideoPreview;
const REPORT: VideoQualityReport = {
  scene_count: 1,
  skipped: null,
  revision: 3,
  findings: [{ scene_index: 0, scene_id: "s1", scene_type: "statement", label: "", kind: "overflow", severity: "dense", message: "The text runs past the frame.", group: "layout" }],
};
const READY_RENDER = {
  id: "r-1",
  project_id: "v-1",
  revision: 3,
  quality: "standard",
  status: "ready",
  size_bytes: 1_000_000,
  file_id: "f-1",
  width: 1920,
  height: 1080,
  duration_ms: 64_000,
  captions_file_id: null,
  poster_file_id: null,
} as unknown as VideoRender;

let current: VideoProject | null = null;
let accountTheme: string | null = null;

function video(overrides: Partial<VideoProject>): VideoProject {
  return {
    id: "v-1",
    user_id: "u-owner",
    title: "Brand film",
    spec: SPEC,
    template: null,
    freestyle: true,
    template_scenes: [],
    revision: 3,
    scene_count: 1,
    estimated_duration_ms: 4000,
    latest_render: null,
    ready_render: null,
    has_public_link: true,
    can_manage: true,
    ...overrides,
  } as unknown as VideoProject;
}

function answer(path: string): unknown {
  if (path === "/video-studio/projects/v-1") return current;
  if (path === "/video-studio/projects/v-1/quality") return REPORT;
  if (path === "/video-studio/templates") return { items: [] };
  if (path === "/auth/me") return { id: "u-owner", theme: accountTheme };
  return {};
}

async function open(project: VideoProject, { previewed = true }: { previewed?: boolean } = {}) {
  current = project;
  renderWithProviders(
    <MemoryRouter initialEntries={["/videos/v-1"]}>
      <Routes>
        <Route path="/videos/:id" element={<VideoEditor />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByTestId("video.editor");
  // The preview is planned once the draft settles: past it, the page has drawn what it draws.
  if (previewed) await waitFor(() => expect(api.post).toHaveBeenCalledWith("/video-studio/projects/v-1/preview", expect.anything()));
}

const qualityRequested = () => vi.mocked(api.get).mock.calls.some(([path]) => path === "/video-studio/projects/v-1/quality");
const previewRequested = () => vi.mocked(api.post).mock.calls.some(([path]) => path === "/video-studio/projects/v-1/preview");

beforeEach(() => {
  accountTheme = null;
  localStorage.clear();
  delete document.documentElement.dataset.mode;
  vi.mocked(api.get).mockClear();
  vi.mocked(api.post).mockClear();
  vi.mocked(api.get).mockImplementation(((path: string) => Promise.resolve(answer(path))) as typeof api.get);
  vi.mocked(api.post).mockImplementation(((path: string, body: { asset?: string } | undefined) =>
    Promise.resolve(path === "/video-studio/media-token" ? { url: `/media/${body?.asset ?? ""}` } : PREVIEW)) as typeof api.post);
});

describe("VideoEditor for a reader", () => {
  it("draws no inspector, no quality check and no editing control", async () => {
    await open(video({ can_manage: false }));

    expect(screen.queryByRole("complementary", { name: "Inspector" })).toBeNull();
    expect(screen.queryByTestId("scene-inspector")).toBeNull();
    expect(screen.queryByTestId("video.editor.quality")).toBeNull();
    expect(qualityRequested()).toBe(false);

    expect(screen.getByRole("heading", { level: 1, name: "Brand film" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Title (heading)" })).toBeNull();
    expect(screen.queryByText("Read only")).toBeNull();
    for (const name of ["Undo", "Redo", RENDER_VERB, "Share", "Cut at the playhead", "Delete the scene", "Add a scene"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByTestId("video.editor.publicLink")).toBeNull();
    // The timeline stays, to watch: its zoom is still there.
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
  });

  it("offers the download once a render exists, and no disabled one before", async () => {
    await open(video({ can_manage: false }));
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
  });

  it("offers the current render's download", async () => {
    await open(video({ can_manage: false, ready_render: READY_RENDER }), { previewed: false });
    expect(screen.getByRole("button", { name: "Download the video" })).toBeEnabled();
  });

  it("watches the render once there is one, like the public link, and asks for no preview", async () => {
    await open(video({ can_manage: false, ready_render: READY_RENDER }), { previewed: false });

    const player = await screen.findByTestId("video.editor.rendered");
    await waitFor(() => expect(player.querySelector("video")).toHaveAttribute("src", "/media/video"));
    expect(screen.getByText(/1920x1080/)).toBeInTheDocument();
    // The edit's tools to watch it are not there: the render is what plays.
    expect(screen.queryByRole("button", { name: "Zoom in" })).toBeNull();
    expect(screen.queryByTestId("video.editor.notRendered")).toBeNull();
    expect(previewRequested()).toBe(false);
    // Space is the player's, not the editor's transport.
    expect(fireEvent.keyDown(document.body, { key: " " })).toBe(true);
  });

  it("says the preview is the edit's while nothing is rendered", async () => {
    await open(video({ can_manage: false }));
    expect(screen.getByTestId("video.editor.notRendered")).toBeInTheDocument();
    expect(screen.queryByTestId("video.editor.rendered")).toBeNull();
    // Nothing to play, and nothing a reader could do to make it.
    expect(screen.queryByTestId("video.editor.play")).toBeNull();
  });

  it("plays the render full screen from the bar", async () => {
    await open(video({ can_manage: false, ready_render: READY_RENDER }), { previewed: false });
    fireEvent.click(screen.getByRole("button", { name: "Play the current render, full screen" }));
    const theater = screen.getByRole("dialog", { name: "Brand film" });
    await waitFor(() => expect(theater.querySelector("video")).toHaveAttribute("src", "/media/video"));
  });

  it("leaves the browser's shortcuts alone and never marks the page unsaved", async () => {
    await open(video({ can_manage: false }));

    expect(fireEvent.keyDown(document.body, { key: "z", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "s", ctrlKey: true })).toBe(true);
    const leave = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(leave);
    expect(leave.defaultPrevented).toBe(false);
  });
});

// Outside AppLayout, the editor applies the account theme itself:
// opened directly with a light preference and a dark cache, it paints light.
describe("VideoEditor theme", () => {
  it("follows the account theme preference when opened directly", async () => {
    localStorage.setItem("copilot-theme", "dark");
    accountTheme = "light";
    await open(video({ can_manage: true }));
    await waitFor(() => expect(document.documentElement).toHaveAttribute("data-mode", "light"));
    expect(localStorage.getItem("copilot-theme")).toBe("light");
  });

  it("stays dark for a dark preference over a light cache", async () => {
    localStorage.setItem("copilot-theme", "light");
    document.documentElement.dataset.mode = "light";
    accountTheme = "dark";
    await open(video({ can_manage: true }));
    await waitFor(() => expect(document.documentElement).not.toHaveAttribute("data-mode"));
    expect(localStorage.getItem("copilot-theme")).toBe("dark");
  });
});

describe("VideoEditor for an editor", () => {
  it("draws the inspector, the quality check and the editing controls", async () => {
    await open(video({ can_manage: true }));

    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeInTheDocument();
    expect(screen.getByTestId("scene-inspector")).toBeInTheDocument();
    expect(await screen.findByTestId("video.editor.quality")).toBeInTheDocument();

    expect(screen.getByRole("textbox", { name: "Title (heading)" })).toHaveValue("Brand film");
    for (const name of ["Undo", "Redo", RENDER_VERB, "Share", "Cut at the playhead", "Delete the scene", "Add a scene"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByTestId("video.editor.publicLink")).toBeInTheDocument();
    // Not rendered yet: the download and Play say so instead of disappearing.
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Not rendered yet: render the video to play it." })).toBeDisabled();
  });

  // At 1024 with the Studio panel open the template chip ran
  // under Play and "Saved" under Render. The chip leaves a bar under 64rem and
  // truncates; the save state keeps only its icon under 56rem, its word read aloud.
  it("lets the template chip and the save state give way on a narrow bar", async () => {
    await open(video({ can_manage: true }));

    // Tiers read from data attributes, never class names (frontend/AGENTS.md); the
    // widths themselves are measured in the UX lab.
    expect(screen.getByTestId("video.editor.templateChip")).toHaveAttribute("data-visible-from", "5xl");

    const saveState = screen.getByTestId("video.editor.saveState");
    const word = within(saveState).getByTestId("video.editor.saveStateWord");
    expect(word).toHaveAttribute("data-visible-from", "4xl");
    expect(word.textContent).toMatch(/Saved|Unsaved changes|Saving\.\.\.|Not saved/);
  });

  it("keeps editing the preview when the video is rendered", async () => {
    await open(video({ can_manage: true, ready_render: READY_RENDER }));
    expect(screen.queryByTestId("video.editor.rendered")).toBeNull();
    expect(screen.getByRole("button", { name: "Cut at the playhead" })).toBeInTheDocument();
  });

  it("plays the current render in the theater, and Render opens the renders", async () => {
    await open(video({ can_manage: true, ready_render: READY_RENDER }));
    fireEvent.click(screen.getByRole("button", { name: "Play the current render, full screen" }));
    const theater = screen.getByRole("dialog", { name: "Brand film" });
    await waitFor(() => expect(theater.querySelector("video")).toHaveAttribute("src", "/media/video"));
    fireEvent.keyDown(theater, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Brand film" })).toBeNull();

    // Rendering is the second verb: it opens the inspector's renders tab, and says so (it starts nothing itself:
    // the quality is picked and the render started, or refused by the gate, in that tab).
    expect(screen.queryByTestId("renders-panel")).toBeNull();
    const verb = screen.getByTestId("video.editor.render");
    expect(verb).toHaveAccessibleName(RENDER_VERB);
    fireEvent.click(verb);
    expect(screen.getByTestId("renders-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Renders" })).toHaveAttribute("aria-pressed", "true");
  });
});
