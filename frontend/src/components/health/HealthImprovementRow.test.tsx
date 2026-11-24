// An agent's Platform Health improvement is one compact row (#101 S10): the switch,
// the count of proposals awaiting approval, and the details in a dialog.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { HealthImprovementRow } from "./HealthImprovementRow";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

function answer(optIn: { opt_in: boolean; can_manage: boolean; eligible: boolean }, pending: number) {
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url.startsWith("/health/improvements")) return { items: [], total: pending };
    if (url.endsWith("/opt-in")) return optIn;
    return [];
  });
}

describe("the Platform Health row of an agent", () => {
  it("switches the opt-in and counts the proposals awaiting approval", async () => {
    answer({ opt_in: false, can_manage: true, eligible: true }, 2);
    vi.mocked(api.put).mockResolvedValue({ opt_in: true, can_manage: true });
    renderWithProviders(<HealthImprovementRow agentId="a-1" canManage />);

    expect(await screen.findByText("2 proposals await approval")).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith("/health/improvements?agent_id=a-1&status=pending_approval&limit=1&offset=0");
    fireEvent.click(screen.getByRole("switch", { name: "Opt in to improvement" }));
    // Off to on asks first, with the consent notice, and saves nothing before the confirm.
    expect(await screen.findByTestId("health.consent-notice")).toBeTruthy();
    expect(api.put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/agents/a-1/opt-in", { opt_in: true }));
  });

  it("saves nothing when the consent confirm is cancelled", async () => {
    answer({ opt_in: false, can_manage: true, eligible: true }, 0);
    renderWithProviders(<HealthImprovementRow agentId="a-4" canManage />);

    fireEvent.click(await screen.findByRole("switch", { name: "Opt in to improvement" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("health.consent-notice")).toBeNull());
    expect(api.put).not.toHaveBeenCalled();
    // Off with nothing pending: no count badge.
    expect(screen.queryByText("No proposal awaits approval")).toBeNull();
  });

  it("turns off at once, without a confirm", async () => {
    answer({ opt_in: true, can_manage: true, eligible: true }, 0);
    vi.mocked(api.put).mockResolvedValue({ opt_in: false, can_manage: true });
    renderWithProviders(<HealthImprovementRow agentId="a-5" canManage />);

    expect(await screen.findByText("No proposal awaits approval")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Opt in to improvement" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/agents/a-5/opt-in", { opt_in: false }));
    expect(screen.queryByTestId("health.consent-notice")).toBeNull();
  });

  it("is read only, without count or details, for a viewer who cannot manage the agent", async () => {
    answer({ opt_in: true, can_manage: false, eligible: true }, 3);
    renderWithProviders(<HealthImprovementRow agentId="a-2" canManage={false} />);

    const toggle = await screen.findByRole("switch", { name: "Opt in to improvement" });
    expect(toggle.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Only the agent's managers can change this.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Proposals and cycles" })).toBeNull();
    expect(screen.queryByText(/await approval/)).toBeNull();
  });

  it("is absent for a Platform Health agent", async () => {
    answer({ opt_in: false, can_manage: false, eligible: false }, 0);
    const { container } = renderWithProviders(<HealthImprovementRow agentId="a-3" canManage />);
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.querySelector("[data-testid='agent.health-row']")).toBeNull();
  });
});
