/**
 * The sharing card of an agent's General tab is read-only where the viewer
 * cannot manage the agent (#2203): a built-in agent in XTM One mode has
 * `canManage` false for everyone, administrators included, and the server
 * answers 403 to a sharing write. The card is still rendered for an
 * administrator (it names the audience), so its switch must be disabled.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";

const source = readFileSync(join(__dirname, "GeneralSection.tsx"), "utf8");

describe("agent General tab: sharing gated on canManage (#2203)", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.get.mockResolvedValue([]);
  });

  it("passes canManage to the sharing card's disabled state", () => {
    const start = source.indexOf("<PrincipalSharingCard");
    expect(start).toBeGreaterThan(-1);
    const card = source.slice(start, source.indexOf("/>", start));
    expect(card).toContain("disabled={isPending || !canManage}");
    expect(card).toContain("restrictionCopy={canManage ? undefined : READ_ONLY_RESTRICTION_COPY}");
  });

  it("states the read-only reason instead of the toggle hint when the viewer cannot manage", async () => {
    renderWithProviders(
      <PrincipalSharingCard
        sharedGroupIds={null}
        onChange={vi.fn()}
        withPermissions={false}
        isCompanyManaged
        disabled
        restrictionCopy={{
          title: "Restrict access",
          restricted: "Only the people and groups below can see this. Access to a built-in agent cannot be changed.",
          open: "Visible to everyone on the platform. Access to a built-in agent cannot be restricted.",
          empty: "No one is listed yet, so only admins and the owner can reach this.",
        }}
      />,
    );
    expect(await screen.findByText("Visible to everyone on the platform. Access to a built-in agent cannot be restricted.")).toBeInTheDocument();
    expect(screen.queryByText(/Toggle on to limit it/)).not.toBeInTheDocument();
    expect(source).toContain('open: "Visible to everyone on the platform. Access to a built-in agent cannot be restricted."');
  });

  it("disables the sharing switch and sends nothing when the viewer cannot manage", async () => {
    const onChange = vi.fn();
    renderWithProviders(<PrincipalSharingCard sharedGroupIds={null} onChange={onChange} withPermissions={false} disabled />);
    const toggle = await screen.findByRole("switch", { name: "Share with people and groups" });
    expect(toggle).toBeDisabled();
    await userEvent.setup().click(toggle);
    expect(onChange).not.toHaveBeenCalled();
  });
});
