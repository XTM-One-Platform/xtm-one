// Sharing a dashboard: only the sharing fields travel, never the spec; the
// company switch is an admin's; the dialog says live widgets are read with
// each viewer's own access.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const { updateSharing, auth } = vi.hoisted(() => ({ updateSharing: vi.fn(), auth: { isAdmin: false } }));

vi.mock("@/hooks/api/useCustomDashboardApi", () => ({
  useUpdateDashboardSharing: () => ({ mutateAsync: updateSharing, isPending: false }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));
// The people and groups picker has its own tests: here it grants one group when asked.
vi.mock("@/components/ui/principal-sharing-card", () => ({
  PrincipalSharingCard: ({
    onChange,
    onPermissionsChange,
  }: {
    onChange: (v: string[]) => void;
    onPermissionsChange: (v: Record<string, string>) => void;
  }) => (
    <button
      type="button"
      onClick={() => {
        onChange(["g-1"]);
        onPermissionsChange({ "g-1": "read_only" });
      }}
    >
      Share with Analysts
    </button>
  ),
}));

import { DashboardShareDialog } from "./DashboardShareDialog";

const DASHBOARD = {
  id: "d-1",
  title: "Board brief",
  spec: { title: "Board brief", widgets: [] },
  revision: 3,
  is_company_managed: false,
  access_restricted: false,
  shared_group_ids: [],
  shared_group_permissions: {},
  shared_user_ids: [],
  shared_user_permissions: {},
  can_manage: true,
} as unknown as CustomDashboard;

beforeEach(() => {
  updateSharing.mockReset().mockResolvedValue({});
  auth.isAdmin = false;
});

describe("DashboardShareDialog", () => {
  it("saves the sharing fields alone, once something changed", async () => {
    renderWithProviders(<DashboardShareDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    expect(screen.getByText(/sharing the dashboard never shares your data/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByRole("switch", { name: "Company dashboard" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Share with Analysts" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateSharing).toHaveBeenCalledTimes(1));
    const sent = updateSharing.mock.calls[0][0];
    expect(sent).toMatchObject({
      is_company_managed: false,
      shared_group_ids: ["g-1"],
      shared_group_permissions: { "g-1": "read_only" },
    });
    expect(sent).not.toHaveProperty("spec");
    expect(sent).not.toHaveProperty("revision");
  });

  it("offers an admin to make it a company dashboard", () => {
    auth.isAdmin = true;
    renderWithProviders(<DashboardShareDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    expect(screen.getByRole("switch", { name: "Company dashboard" })).toBeInTheDocument();
  });
});
