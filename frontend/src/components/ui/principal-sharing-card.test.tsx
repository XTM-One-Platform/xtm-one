// Which tiers the sharing card offers: the strongest one (`config_with_chats`) where it means more than
// editing - an agent's chats, or an entity whose own labels name it (a dashboard's "Can manage").

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { DASHBOARD_SHARE_PERMISSION_DESCRIPTIONS, DASHBOARD_SHARE_PERMISSION_LABELS, PRESENTATION_SHARE_PERMISSION_LABELS } from "@/lib/constants";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { PrincipalSharingCard } from "./principal-sharing-card";

function draw(labels: Record<string, string>, descriptions?: Record<string, string>) {
  return renderWithProviders(
    <PrincipalSharingCard
      sharedGroupIds={["g-1"]}
      onChange={vi.fn()}
      sharedGroupPermissions={{ "g-1": "config" }}
      onPermissionsChange={vi.fn()}
      entityType="dashboard"
      permissionLabels={labels}
      permissionDescriptions={descriptions}
    />,
  );
}

async function tiers() {
  await userEvent.setup().click(await screen.findByRole("combobox"));
  return within(await screen.findByRole("listbox")).getAllByRole("option").map((option) => option.textContent?.split("—")[0].trim());
}

describe("PrincipalSharingCard tiers", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.get.mockResolvedValue([{ id: "g-1", name: "Analysts", member_count: 2 }]);
  });

  it("offers a dashboard its three tiers, Can manage included", async () => {
    draw(DASHBOARD_SHARE_PERMISSION_LABELS, DASHBOARD_SHARE_PERMISSION_DESCRIPTIONS);
    expect(await tiers()).toEqual(["Can view", "Can edit", "Can manage"]);
  });

  it("offers an entity whose labels stop at editing two tiers", async () => {
    draw(PRESENTATION_SHARE_PERMISSION_LABELS);
    expect(await tiers()).toEqual(["Can view", "Can edit"]);
  });
});

describe("PrincipalSharingCard restriction switch", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.get.mockResolvedValue([]);
  });

  it("names the switch from the restriction title and shows its help", async () => {
    renderWithProviders(
      <PrincipalSharingCard
        sharedGroupIds={null}
        onChange={vi.fn()}
        isCompanyManaged
        withPermissions={false}
        restrictionCopy={{ title: "Never use for people and groups", restricted: "Excluded below.", open: "No one is excluded.", empty: "None." }}
      />,
    );
    expect(await screen.findByRole("switch", { name: "Never use for people and groups" })).toBeInTheDocument();
    expect(screen.getByText("No one is excluded.")).toBeVisible();
  });

  it("keeps the default name without a restriction copy", async () => {
    renderWithProviders(<PrincipalSharingCard sharedGroupIds={null} onChange={vi.fn()} isCompanyManaged withPermissions={false} />);
    expect(await screen.findByRole("switch", { name: "Restrict access" })).toBeInTheDocument();
  });
});
