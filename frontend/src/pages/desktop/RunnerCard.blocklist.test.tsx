// The Machines page shell block-list reads at full error contrast:
// it painted the error tone at 80% opacity, 3.40:1 on the dark
// card at 11 to 12 px. It carries the error tone;
// tests_unit/warning-text-contrast.test.ts pins the token on source (no opacity
// modifier) and holds the colour at 4.5:1.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Runner } from "@/types";

const idle = { mutate: vi.fn(), isPending: false };
const empty = { data: undefined, isLoading: false, isFetching: false, refetch: vi.fn() };
vi.mock("@/hooks/api/useAuthApi", () => ({
  useMe: () => ({ data: { id: "u-1", role: "member" } }),
  useAuthConfig: () => empty,
}));
vi.mock("@/hooks/api/useRunnerApi", () => ({
  useRunners: () => empty,
  useRunnerCapabilities: () => empty,
  useRunnerDownloadManifest: () => empty,
  useUpdateRunner: () => idle,
  useRevokeRunner: () => idle,
  useDeleteRunner: () => idle,
  useRunnerCommands: () => empty,
  useRunnerJobs: () => empty,
  useStopRunnerCommand: () => idle,
  useApproveRunnerCommand: () => idle,
  useRejectRunnerCommand: () => idle,
  useRunnerEnrollmentKeys: () => empty,
  useCreateRunnerEnrollmentKey: () => idle,
  useDeleteRunnerEnrollmentKey: () => idle,
}));

const { RunnerCard } = await import("./RunnersSection");

const RUNNER: Runner = {
  id: "r-1",
  user_id: "u-1",
  name: "LAB-MACHINE",
  platform: "linux",
  hostname: "lab",
  key_prefix: "xr_",
  permission_mode: "restricted",
  capabilities: [],
  allowed_shell_commands: null,
  blocked_shell_commands: ["rm -rf", "shutdown"],
  allowed_paths: null,
  require_approval: false,
  labels: [],
  tags: [],
  host_facts: null,
  agent_version: null,
  status: "offline",
  online: false,
  last_seen_at: null,
  transport: null,
  is_active: true,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  command_count: 0,
  can_manage: false,
  enrolled_at: null,
  created_at: "2026-11-03T00:00:00+00:00",
  updated_at: "2026-11-03T00:00:00+00:00",
};

describe("RunnerCard shell block-list", () => {
  it("marks the blocked commands with the error tone", () => {
    renderWithProviders(<RunnerCard runner={RUNNER} defaultExpanded />);
    const list = screen.getByText("rm -rf, shutdown");
    expect(list.hasAttribute("data-shell-block-list")).toBe(true);
    expect(list.getAttribute("data-tone")).toBe("error");
  });
});
