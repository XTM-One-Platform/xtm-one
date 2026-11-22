// The runner approval card on the shared decision card (#842, S8): the four
// answers the runner's permission model knows, the standing two behind a
// confirm, each sending the existing approve / reject route with `remember`.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { RunnerApprovalRequest } from "@/types";

const approve = { mutate: vi.fn(), isPending: false };
const reject = { mutate: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useRunnerApi", () => ({
  useApproveRunnerCommand: () => approve,
  useRejectRunnerCommand: () => reject,
}));

const { RunnerApprovalCard, runnerApprovalOptions } = await import("./RunnerApprovalCard");

const REQUEST: RunnerApprovalRequest = {
  command_id: "cmd-1",
  runner_id: "r-1",
  runner_name: "LAB-MACHINE",
  kind: "run_shell",
  summary: "rm -rf /tmp/build",
  expires_in_seconds: 120,
  received_at_ms: Date.now(),
};

const t = (s: string) => s;

function option(key: string) {
  const tile = document.querySelector(`[data-testid="runner-decision-tile"][data-option="${key}"] button`);
  expect(tile, key).not.toBeNull();
  return tile as HTMLElement;
}

describe("RunnerApprovalCard on the decision card", () => {
  beforeEach(() => {
    approve.mutate.mockReset();
    reject.mutate.mockReset();
  });

  it("offers the four answers for a shell command, the standing two behind a confirm", () => {
    const options = runnerApprovalOptions(REQUEST, t as never);
    expect(options.map((o) => o.key)).toEqual(["approve", "always_allow", "reject", "always_block"]);
    expect(options.filter((o) => o.confirm).map((o) => o.key)).toEqual(["always_allow", "always_block"]);
  });

  it("offers only Approve and Reject for a command that is not a shell command", () => {
    const options = runnerApprovalOptions({ ...REQUEST, kind: "read_file" }, t as never);
    expect(options.map((o) => o.key)).toEqual(["approve", "reject"]);
  });

  it("shows the command and sends Approve once without a confirm", () => {
    renderWithProviders(<RunnerApprovalCard request={REQUEST} onDecided={() => {}} />);
    expect(screen.getByTestId("runner-approval-command").textContent).toContain("rm -rf /tmp/build");
    fireEvent.click(option("approve"));
    expect(approve.mutate).toHaveBeenCalledWith({ commandId: "cmd-1", remember: false }, expect.anything());
  });

  it("sends Always block only after its confirm", () => {
    renderWithProviders(<RunnerApprovalCard request={REQUEST} onDecided={() => {}} />);
    fireEvent.click(option("always_block"));
    expect(reject.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("runner-decision-confirm-send"));
    expect(reject.mutate).toHaveBeenCalledWith({ commandId: "cmd-1", remember: true }, expect.anything());
  });
});
