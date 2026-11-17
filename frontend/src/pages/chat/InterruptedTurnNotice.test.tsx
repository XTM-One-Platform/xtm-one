// The interrupted-turn notice the chat panel's transcript footer shows:
// what a cut turn kept, and its Resume unless the platform resumes it itself.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { InterruptedTurnNotice } from "@/pages/chat/InterruptedTurnNotice";
import { renderWithProviders } from "@/testing/providers";

describe("the interrupted-turn notice", () => {
  it("says what was kept and picks the turn up", () => {
    const onResume = vi.fn();
    const onDismiss = vi.fn();
    renderWithProviders(
      <InterruptedTurnNotice
        turn={{ started_at: "2026-10-19T19:30:00Z", interrupted_at: "2026-10-19T20:05:00Z", tool_calls_kept: 7 }}
        onResume={onResume}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByText("This response stopped before it finished")).toBeTruthy();
    expect(screen.getByText(/Its 7 completed tool calls are kept/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("offers nothing to a reader who cannot act", () => {
    renderWithProviders(
      <InterruptedTurnNotice turn={{ started_at: null, interrupted_at: null, tool_calls_kept: 0 }} />,
    );

    expect(screen.getByText(/stopped without finishing it/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says it resumes by itself and offers no Resume to race it while queued", () => {
    const onResume = vi.fn();
    const onDismiss = vi.fn();
    renderWithProviders(
      <InterruptedTurnNotice
        turn={{ started_at: null, interrupted_at: "2026-11-01T11:44:46Z", tool_calls_kept: 67, auto_resume: true }}
        onResume={onResume}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByTestId("interrupted-turn-auto-resume").textContent).toBe(
      "It is resuming by itself: no need to click anything.",
    );
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeTruthy();
  });
});
