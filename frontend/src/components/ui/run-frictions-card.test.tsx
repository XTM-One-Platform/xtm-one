import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { RunFrictionsCard } from "@/components/ui/run-frictions-card";
import { renderWithProviders } from "@/testing/providers";
import type { RunFriction } from "@/types";

function friction(over: Partial<RunFriction>): RunFriction {
  return {
    id: "f1",
    kind: "tool_error",
    tool: "sandbox_grep",
    evidence: "Error: pattern refused",
    count: 1,
    class: "unclassified",
    disposition: null,
    recorded_at: "2026-11-05T03:00:00Z",
    last_seen_at: "2026-11-05T03:00:00Z",
    disposed_at: null,
    ...over,
  };
}

// The run page lists the run's friction ledger (#8528): kind, tool, repeats,
// class and what carries the fix, the undisposed ones counted.
describe("RunFrictionsCard", () => {
  it("lists each friction with its repeats, class and disposition", () => {
    renderWithProviders(
      <RunFrictionsCard
        frictions={[
          friction({ id: "a", count: 2 }),
          friction({
            id: "b",
            kind: "approval_pause",
            tool: "create_github_commit",
            class: "harness",
            disposition: { kind: "work_item", ref: "1234" },
          }),
        ]}
        undisposed={1}
      />,
    );
    expect(screen.getByText("Frictions (2)")).toBeTruthy();
    expect(screen.getByTestId("run-frictions-undisposed").textContent).toBe("1 undisposed");
    expect(screen.getAllByTestId("run-friction-row")).toHaveLength(2);
    expect(screen.getByText("Met 2 times")).toBeTruthy();
    expect(screen.getByText("harness")).toBeTruthy();
    expect(screen.getByText("work_item: 1234")).toBeTruthy();
    expect(screen.getByText("Undisposed")).toBeTruthy();
  });

  it("counts the undisposed from the rows while the run is live, and none when all are disposed", () => {
    const { unmount } = renderWithProviders(<RunFrictionsCard frictions={[friction({})]} undisposed={null} />);
    expect(screen.getByTestId("run-frictions-undisposed").textContent).toBe("1 undisposed");
    unmount();
    renderWithProviders(
      <RunFrictionsCard frictions={[friction({ disposition: { kind: "accepted", ref: null } })]} undisposed={null} />,
      { locale: "fr" },
    );
    expect(screen.queryByTestId("run-frictions-undisposed")).toBeNull();
    expect(screen.getByText("accepted")).toBeTruthy();
    expect(screen.getByText("Frictions (1)")).toBeTruthy();
  });
});
