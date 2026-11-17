// The refusal buttons of the runner approval card read at full error contrast:
// "Always block" painted the error tone at 80% opacity,
// 3.40:1 on the dark card. Both refusals carry the error tone;
// tests_unit/warning-text-contrast.test.ts pins the token on source (no opacity
// modifier) and holds the colour at 4.5:1.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { RunnerApprovalRequest } from "@/types";

const idle = { mutate: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useRunnerApi", () => ({
  useApproveRunnerCommand: () => idle,
  useRejectRunnerCommand: () => idle,
}));

const { RunnerApprovalCard } = await import("./RunnerApprovalCard");

const REQUEST: RunnerApprovalRequest = {
  command_id: "cmd-1",
  runner_id: "r-1",
  runner_name: "LAB-MACHINE",
  kind: "run_shell",
  summary: "rm -rf /tmp/build",
  expires_in_seconds: 120,
  received_at_ms: Date.now(),
};

describe("RunnerApprovalCard refusals", () => {
  it("marks Reject and Always block with the error tone", () => {
    renderWithProviders(
      <RunnerApprovalCard
        request={REQUEST}
        onDecided={() => {}}
      />,
    );
    for (const name of ["Reject", "Always block"]) {
      const button = screen.getByRole("button", { name });
      expect(button.getAttribute("data-tone"), name).toBe("error");
    }
  });
});
