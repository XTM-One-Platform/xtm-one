// The editor's renders: a render saves the draft first and says what it replaced,
// a running one is cancelled from its row, a replaced one says so, and a viewer renders nothing.
// Its scene previews say the size each was drawn at. A ready render that already covers the revision is
// said, nothing started unless asked. An older revision's render in progress is asked about once.
// Every finding a render's checks stored is listed, a kind the build cannot label included.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { VideoProject, VideoRender } from "@/types";
import { RendersPanel } from "./RendersPanel";

const { renders, previews, renderVideo, cancel, cleanup, cleanupPreview } = vi.hoisted(() => ({
  renders: { data: [] as unknown[] },
  previews: { data: [] as unknown[] },
  renderVideo: vi.fn(),
  cancel: vi.fn(),
  cleanup: vi.fn(),
  cleanupPreview: vi.fn(),
}));

vi.mock("@/hooks/api/useVideoStudioApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/api/useVideoStudioApi")>();
  return {
    renderGateReasons: actual.renderGateReasons,
    renderReplaceQuestion: actual.renderReplaceQuestion,
    useVideoRenders: () => ({ data: renders.data }),
    useScenePreviews: () => ({ data: previews.data }),
    useRenderVideo: () => ({ mutateAsync: renderVideo, isPending: false }),
    useCancelRender: () => ({ mutate: cancel, isPending: false }),
    useUpdateRender: () => ({ mutate: vi.fn(), isPending: false }),
    useDeleteRender: () => ({ mutate: vi.fn(), isPending: false }),
    useCleanupRenders: () => ({ mutate: cleanup, isPending: false }),
    useRenderCleanupPreview: (...args: unknown[]) => cleanupPreview(...args),
  };
});
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" }, isAdmin: false }) }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));
vi.mock("@/components/video-studio/video-downloads", () => ({
  renderDownloadItems: () => [],
  useRenderDownload: () => ({ download: vi.fn(), isPending: false }),
}));
vi.mock("@/components/ui/license-gate-dialog", () => ({
  AgenticGateDialog: ({ children }: { children: unknown }) => children,
  AgenticEEBadgeForButton: () => null,
}));

const VIDEO = {
  id: "v-1",
  title: "Launch film",
  can_manage: true,
  ready_render: null,
  render_retention: { failed_cancelled_days: 1, previews_days: 7, superseded_ready_days: 30 },
} as unknown as VideoProject;

/** The dry run of a cleanup: the 4K and a failed attempt go, the newest 1080p stays, the public link's render is refused. */
const PREVIEW = {
  video_id: "v-1",
  dry_run: true,
  select: "all_but_latest_ready",
  keep_quality: null,
  renders: [
    { render_id: "r-4k", quality: "ultra", revision: 26, status: "ready", size_bytes: 80_000_000, action: "delete" },
    { render_id: "r-failed", quality: "high", revision: 29, status: "failed", size_bytes: 2_000_000, action: "delete" },
    { render_id: "r-31", quality: "high", revision: 31, status: "ready", size_bytes: 30_000_000, action: "keep", reason: "latest_ready" },
    { render_id: "r-28", quality: "high", revision: 28, status: "ready", size_bytes: 29_000_000, action: "refuse", reason: "public_link" },
  ],
  unknown_render_ids: [],
  would_free: { renders: 2, bytes: 82_000_000 },
  serves: { current: null, current_changed: false, public_link: true, latest_ready: {} },
};

function render(fields: Partial<VideoRender>): VideoRender {
  return {
    id: "r-1",
    project_id: "v-1",
    revision: 3,
    quality: "high",
    status: "ready",
    progress: 100,
    stage: null,
    width: 1920,
    height: 1080,
    fps: 60,
    duration_ms: 40_000,
    size_bytes: 12_000_000,
    notes: [],
    keep: false,
    user_id: "u-1",
    error: null,
    superseded_by_id: null,
    is_current: false,
    expires_at: null,
    created_at: null,
    ...fields,
  } as VideoRender;
}

/** The button that starts a render: its section's header is a button named after it too. */
function renderButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /^Render( again)?$/ }).filter((button) => !button.hasAttribute("aria-expanded"));
}

function panel(video: VideoProject = VIDEO, onBeforeRender = vi.fn().mockResolvedValue(true)) {
  renderWithProviders(<RendersPanel video={video} onBeforeRender={onBeforeRender} />);
  return onBeforeRender;
}

beforeEach(() => {
  renders.data = [];
  previews.data = [];
  renderVideo.mockReset().mockResolvedValue({ id: "r-2", superseded_render_ids: [] });
  cancel.mockReset();
  cleanup.mockReset();
  cleanupPreview.mockReset().mockReturnValue({ data: PREVIEW, isLoading: false, isFetching: false, error: null });
});

describe("RendersPanel", () => {
  it("saves the draft before it renders, and starts nothing when the save refuses", async () => {
    const refused = panel(VIDEO, vi.fn().mockResolvedValue(false));
    fireEvent.click(renderButtons()[0]);
    await waitFor(() => expect(refused).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Fix the video first")).toBeInTheDocument();
    expect(renderVideo).not.toHaveBeenCalled();
  });

  it("says a new render replaced the one in progress", async () => {
    renderVideo.mockResolvedValue({ id: "r-2", superseded_render_ids: ["r-1"] });
    renders.data = [render({ status: "running", progress: 40, stage: "Drawing frames" })];
    panel();
    expect(screen.getByText("A render is in progress: starting a new one cancels it.")).toBeInTheDocument();
    fireEvent.click(renderButtons()[0]);
    await waitFor(() => expect(renderVideo).toHaveBeenCalledWith({ projectId: "v-1", quality: "high", force: false }));
    expect(await screen.findByText("The render in progress was cancelled: this one replaces it.")).toBeInTheDocument();
  });

  const REPLACE = {
    status: 409,
    detail: { code: "render_replace", revision: 100, renders: [{ id: "r-1", revision: 99, quality: "ultra", progress: 35 }] },
  };
  const QUESTION =
    "The render of revision 99 is 35% done. Cancel it and render revision 100? The frames it drew of the scenes this revision leaves unchanged are kept for the new render.";

  it("asks once before it cancels an older revision's render, then renders replacing it", async () => {
    renderVideo.mockRejectedValueOnce(REPLACE).mockResolvedValueOnce({ id: "r-2", superseded_render_ids: ["r-1"] });
    panel();
    fireEvent.click(renderButtons()[0]);
    expect(await screen.findByText(QUESTION)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel it and render" }));
    await waitFor(() =>
      expect(renderVideo).toHaveBeenLastCalledWith({ projectId: "v-1", quality: "high", force: false, replace: true }),
    );
    expect(await screen.findByText("Render started")).toBeInTheDocument();
    expect(renderVideo).toHaveBeenCalledTimes(2);
  });

  it("keeps the older revision's render drawing when the author says so", async () => {
    renderVideo.mockRejectedValueOnce(REPLACE);
    panel();
    fireEvent.click(renderButtons()[0]);
    expect(await screen.findByText(QUESTION)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep it drawing" }));
    await waitFor(() => expect(screen.queryByText(QUESTION)).not.toBeInTheDocument());
    expect(renderVideo).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("The render did not start")).not.toBeInTheDocument();
  });

  it("lists why the quality gate refused the render, and renders anyway only when asked", async () => {
    renderVideo
      .mockRejectedValueOnce({
        status: 409,
        detail: { code: "render_gate", reasons: [{ code: "open_findings", message: "Scene open: the inserted picture flickers.", scene_id: "open" }] },
      })
      .mockResolvedValueOnce({ id: "r-2", superseded_render_ids: [] });
    panel();
    fireEvent.click(screen.getByTestId("video.editor.render.start"));
    const gate = await screen.findByTestId("video.editor.render.gate");
    expect(gate).toHaveTextContent("Scene open: the inserted picture flickers.");
    expect(screen.queryByText("The render did not start")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("video.editor.render.anyway"));
    await waitFor(() => expect(renderVideo).toHaveBeenLastCalledWith({ projectId: "v-1", quality: "high", force: true }));
    expect(await screen.findByText("Render started")).toBeInTheDocument();
  });

  it("says a ready render already covers the revision, and renders it again only when asked", async () => {
    renderVideo.mockResolvedValueOnce({ id: "r-1", superseded_render_ids: [], covered_from: 3 });
    panel();
    fireEvent.click(renderButtons()[0]);
    expect(await screen.findByText("Already rendered")).toBeInTheDocument();
    expect(screen.queryByText("Render started")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Render anyway" }));
    await waitFor(() => expect(renderVideo).toHaveBeenLastCalledWith({ projectId: "v-1", quality: "high", force: true }));
    expect(await screen.findByText("Render started")).toBeInTheDocument();
  });

  it("says when a running render ends: its pre-flight's estimate, then the pace its frames measured", () => {
    const at = "2026-10-23T11:39:00Z";
    const time = new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(new Date(at));
    renders.data = [render({ status: "running", progress: 20, stage: "Drawing frames", eta: { source: "preflight", at, expected_seconds: 1920 } })];
    const { unmount } = renderWithProviders(<RendersPanel video={VIDEO} onBeforeRender={vi.fn()} />);
    expect(screen.getByTestId("video.render.eta")).toHaveTextContent(`Ends about ${time}, as its pre-flight estimated`);
    unmount();
    renders.data = [
      render({ status: "running", progress: 24, stage: "Drawing frames", eta: { source: "measured", at, expected_seconds: 1920, drawn: 846, frames: 8462, per_second: 2.7 } }),
    ];
    panel();
    const eta = screen.getByTestId("video.render.eta");
    expect(eta).toHaveAttribute("data-source", "measured");
    expect(eta).toHaveTextContent(`Ends about ${time}, at the pace its first 846 frames measured`);
  });

  it("cancels a running render from its row, and says a replaced one was replaced", () => {
    renders.data = [
      render({ id: "r-2", status: "running", progress: 40, stage: "Drawing frames" }),
      render({ id: "r-1", status: "cancelled", superseded_by_id: "r-2", error: "Replaced by a newer render of the same video (`r-2`)." }),
    ];
    panel();
    fireEvent.click(screen.getByRole("button", { name: "Cancel render" }));
    expect(cancel).toHaveBeenCalledWith("r-2");
    expect(screen.getByTestId("video.editor.render.superseded")).toHaveTextContent("Replaced by a newer render.");
  });

  it("marks the current render, says what keeps it and what the retention keeps", () => {
    renders.data = [render({ is_current: true, expires_at: null, kept_by: "latest_ready" })];
    panel();
    expect(screen.getByTestId("video.editor.render.current")).toBeInTheDocument();
    expect(screen.getByTestId("video.editor.render.kept-by")).toHaveTextContent("Kept: the newest ready render at this quality.");
    expect(
      screen.getByText(
        "Failed and cancelled renders are kept 1 day, scene previews 7 days and older ready renders 30 days. The newest ready render of each quality, the current render and the ones you keep are never deleted.",
      ),
    ).toBeInTheDocument();
  });

  it("cleans up renders from the dry run of what goes, the public link's render only once confirmed", async () => {
    renders.data = [render({ id: "r-31" })];
    panel();
    fireEvent.click(screen.getByTestId("video.editor.cleanup.open"));
    expect(cleanupPreview).toHaveBeenLastCalledWith("v-1", { select: "all_but_latest_ready", confirm: false }, true);
    const items = screen.getAllByTestId("video.editor.cleanup.item").map((node) => node.textContent);
    expect(items).toEqual([
      "DeletedUltra (4K, 60 fps) - revision 2680.0 MB",
      "DeletedHigh (1080p, 60 fps) - revision 292.0 MB",
      "KeptHigh (1080p, 60 fps) - revision 31 - The newest ready render at its quality30.0 MB",
      "RefusedHigh (1080p, 60 fps) - revision 28 - The public link plays it29.0 MB",
    ]);
    expect(screen.getByTestId("video.editor.cleanup.total")).toHaveTextContent("2 renders go, freeing 82.0 MB.");
    // The public link's render is refused: the confirmation is offered, and the dry run follows it.
    fireEvent.click(screen.getByRole("switch", { name: "Confirm" }));
    expect(cleanupPreview).toHaveBeenLastCalledWith("v-1", { select: "all_but_latest_ready", confirm: true }, true);
    fireEvent.click(screen.getByTestId("video.editor.cleanup.delete"));
    expect(cleanup).toHaveBeenCalledWith(
      { projectId: "v-1", select: "all_but_latest_ready", confirm: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("offers no cleanup to a viewer with no render of their own", () => {
    renders.data = [render({ user_id: "someone-else" })];
    panel({ ...VIDEO, can_manage: false } as VideoProject);
    expect(screen.queryByTestId("video.editor.cleanup.open")).not.toBeInTheDocument();
  });

  it("lists the scene previews with the size each was drawn at under it", () => {
    previews.data = [
      render({ id: "p-2", scene_id: "s04", quality: "standard", width: 1920, height: 1080, fps: 30 }),
      render({ id: "p-1", scene_id: "s03", quality: "draft", width: 1280, height: 720, fps: 30 }),
    ];
    panel();
    fireEvent.click(screen.getByRole("button", { name: "Scene previews" }));
    const sizes = screen.getAllByTestId("video.editor.scene-preview.size").map((node) => node.textContent);
    expect(sizes).toEqual(["Previewed at 1920x1080, 30 fps", "Previewed at 1280x720, 30 fps"]);
    expect(screen.getByText("Scene s04")).toBeInTheDocument();
  });

  it("words a picture finding, and lists a kind this build does not know without taking the panel down", () => {
    const checks = {
      status: "failed",
      findings: [
        { kind: "insert_soft", severity: "fail", at: 2, end: 3.5, scene_id: "s02", message: "the insert is soft" },
        { kind: "insert_from_a_newer_backend", severity: "warn", at: 4, end: null, scene_id: null, message: "something new" },
      ],
    } as unknown as VideoRender["checks"];
    renders.data = [render({ status: "failed", checks })];
    panel();
    const findings = screen.getByTestId("video.editor.render.findings");
    expect(findings).toHaveTextContent("Screen insert soft (s02): the insert is soft");
    expect(findings).toHaveTextContent("insert_from_a_newer_backend: something new");
  });

  it("words a finding from its code and params, and falls back to its message for a code this build does not know", () => {
    const checks = {
      status: "findings",
      findings: [
        { kind: "frozen", severity: "warn", at: 1, end: 2.5, scene_id: "s02", message: "backend words", code: "frozen", params: { seconds: 1.5, start: "0:01.00", end: "0:02.50" } },
        { kind: "double_image", severity: "warn", at: 4, end: 4.1, scene_id: null, message: "backend words too", code: "double_image_v2", params: {} },
      ],
    } as unknown as VideoRender["checks"];
    renders.data = [render({ status: "failed", checks })];
    panel();
    const findings = screen.getByTestId("video.editor.render.findings");
    expect(findings).toHaveTextContent("Frozen picture (s02): the picture is frozen for 1.5 s (0:01.00 to 0:02.50)");
    expect(findings).not.toHaveTextContent("backend words (");
    expect(findings).toHaveTextContent("Two pictures in one frame: backend words too");
  });

  it("formats a finding's seconds and a render's age in the reader's language", () => {
    const checks = {
      status: "findings",
      findings: [
        { kind: "frozen", severity: "warn", at: 1, end: 2.5, scene_id: "s02", message: "backend words", code: "frozen", params: { seconds: 1.5, start: "0:01.00", end: "0:02.50" } },
      ],
    } as unknown as VideoRender["checks"];
    renders.data = [render({ status: "failed", checks, created_at: new Date().toISOString() })];
    renderWithProviders(<RendersPanel video={VIDEO} onBeforeRender={vi.fn().mockResolvedValue(true)} />, { locale: "fr" });
    expect(screen.getByTestId("video.editor.render.findings")).toHaveTextContent("1,5 s (0:01.00 à 0:02.50)");
    expect(screen.getByText(/révision 3 - maintenant/)).toBeInTheDocument();
    expect(screen.queryByText(/just now/)).not.toBeInTheDocument();
  });

  it("counts a term's takes that failed beside the takes kept, never reading them as all passed", () => {
    const checks = {
      status: "passed",
      findings: [],
      pronunciation: {
        status: "passed",
        terms: [
          {
            term: "OpenAEV",
            occurrences: 3,
            passed: 3,
            heard: ["OpenAEV"],
            attempts_failed: [{ scene_id: "st-demo", times: 1, heard: ["OpenA"] }],
          },
          { term: "CTEM", occurrences: 1, passed: 1, heard: ["see tem"] },
        ],
      },
    } as unknown as VideoRender["checks"];
    renders.data = [render({ checks })];
    panel();
    const line = screen.getByTestId("video.editor.render.pronunciation");
    expect(line).toHaveTextContent("OpenAEV 3/3 \u00b7 1 attempt failed on scene st-demo (heard \u201cOpenA\u201d), CTEM 1/1");
    expect(screen.getAllByTestId("video.editor.render.pronunciation.failed-takes")).toHaveLength(1);
  });

  it("shows no scene previews section for a video never previewed", () => {
    panel();
    expect(screen.queryByRole("button", { name: "Scene previews" })).not.toBeInTheDocument();
  });

  it("offers a viewer no render, and points them to a copy of their own", () => {
    panel({ ...VIDEO, can_manage: false } as VideoProject);
    expect(renderButtons()).toEqual([]);
    expect(screen.getByText("Only the video's editors can render it. Duplicate it to render your own version.")).toBeInTheDocument();
  });
});
