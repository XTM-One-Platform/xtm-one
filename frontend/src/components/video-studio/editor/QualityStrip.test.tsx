// The quality strip shows what the audience would see go wrong, and the pictures and
// the sound: those findings are read without a browser, so they stay when the layout
// could not be measured, with their own severity words. `Fix with my assistant` primes
// the studio with them in the words the agent reads after a save (`format_quality_report`).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { VideoQualityFinding, VideoQualityReport } from "@/types";
import { QualityStrip } from "./QualityStrip";

const platform = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => platform.aiEnabled,
}));

const SHOT_FAILED: VideoQualityFinding = {
  scene_index: 0,
  scene_id: "broken",
  scene_type: "clip",
  label: "",
  kind: "shot_state",
  severity: "overloaded",
  message: 'its AI shot "Opening drone shot" failed - generate it again in place',
  group: "content",
};

function report(changes: Partial<VideoQualityReport>): VideoQualityReport {
  return { scene_count: 3, skipped: null, revision: 2, findings: [SHOT_FAILED], ...changes };
}

describe("QualityStrip", () => {
  beforeEach(() => {
    platform.aiEnabled = true;
  });

  it("shows the pictures' findings when the layout was not measured", () => {
    const open = vi.fn();
    renderWithProviders(<QualityStrip report={report({ skipped: "the headless browser is not available" })} onOpenScene={open} />);
    fireEvent.click(screen.getByText("Quality check: 1 thing the audience would see"));
    expect(screen.getByText("Blocks the render")).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Opening drone shot/));
    expect(open).toHaveBeenCalledWith("broken");
  });

  it("keeps the layout's own severity words for a layout finding", () => {
    // The backend says which group a finding is in: the editor keeps no list of kinds of its own.
    const layout: VideoQualityFinding = { ...SHOT_FAILED, group: "layout", message: "the badge covers the title" };
    renderWithProviders(<QualityStrip report={report({ findings: [layout] })} onOpenScene={() => undefined} />);
    fireEvent.click(screen.getByText("Quality check: 1 thing the audience would see"));
    expect(screen.getByText("Unreadable")).toBeInTheDocument();
  });

  it("shows nothing for a clean report", () => {
    renderWithProviders(<QualityStrip report={report({ findings: [] })} onOpenScene={() => undefined} />);
    expect(screen.queryByTestId("video.editor.quality")).toBeNull();
  });

  it("hands the findings to the assistant as the agent's own report names them, without opening the list", () => {
    const fix = vi.fn();
    const outro: VideoQualityFinding = {
      scene_index: null,
      scene_id: null,
      scene_type: "outro",
      label: "Thank you",
      kind: "overlap",
      severity: "dense",
      message: "the watermark covers the logo",
      group: "layout",
    };
    renderWithProviders(
      <QualityStrip
        report={report({ findings: [{ ...SHOT_FAILED, label: "Opening" }, outro] })}
        onOpenScene={() => undefined}
        onFixWithAssistant={fix}
      />,
    );
    fireEvent.click(screen.getByTestId("video.editor.quality.fix"));
    expect(fix).toHaveBeenCalledWith(
      [
        "Fix this video. The quality check found these issues; fix them and save again:",
        // `test_video_studio_quality.py` checks these are the lines `format_quality_report` writes.
        `- scene 0 [broken] clip "Opening" (overloaded): its AI shot "Opening drone shot" failed - generate it again in place`,
        `- the template's outro scene "Thank you" (dense): the watermark covers the logo`,
      ].join("\n"),
    );
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("primes at most the findings the agent's report lists, and says how many more", () => {
    const fix = vi.fn();
    const findings = Array.from({ length: 10 }, (_, i) => ({ ...SHOT_FAILED, scene_index: i, scene_id: `s${i}` }));
    renderWithProviders(<QualityStrip report={report({ findings })} onOpenScene={() => undefined} onFixWithAssistant={fix} />);
    fireEvent.click(screen.getByTestId("video.editor.quality.fix"));
    const lines = (fix.mock.calls[0][0] as string).split("\n");
    expect(lines).toHaveLength(1 + 8 + 1);
    expect(lines[8]).toContain("scene 7 [s7]");
    expect(lines[9]).toBe("- ...and 2 more; fix the above and save, the next check lists the rest.");
  });

  it("offers no fix without a studio to hand it to", () => {
    renderWithProviders(<QualityStrip report={report({})} onOpenScene={() => undefined} />);
    expect(screen.queryByTestId("video.editor.quality.fix")).toBeNull();
  });

  it("disables the fix when AI is disabled on the platform", () => {
    platform.aiEnabled = false;
    const fix = vi.fn();
    renderWithProviders(<QualityStrip report={report({})} onOpenScene={() => undefined} onFixWithAssistant={fix} />);
    const button = screen.getByTestId("video.editor.quality.fix");
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(fix).not.toHaveBeenCalled();
  });
});
