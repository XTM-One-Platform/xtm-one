// A flow's managers opt it in to improvement on the flow's own route; a viewer
// who cannot manage it reads the state and cannot change it.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { HealthOptInCard } from "./HealthOptInCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));

describe("the Platform Health opt-in of a flow", () => {
  it("reads and writes the flow's opt-in", async () => {
    vi.mocked(api.get).mockResolvedValue({ opt_in: false, can_manage: true, eligible: true });
    vi.mocked(api.put).mockResolvedValue({ opt_in: true, can_manage: true });
    renderWithProviders(<HealthOptInCard targetType="flow" targetId="f-1" />);

    fireEvent.click(await screen.findByRole("switch", { name: "Opt in to improvement" }));

    expect(api.get).toHaveBeenCalledWith("/health/flows/f-1/opt-in");
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/flows/f-1/opt-in", { opt_in: true }));
    expect(await screen.findByText("Platform Health may now improve this flow")).toBeTruthy();
  });

  it("is read only for a viewer who cannot manage the flow", async () => {
    vi.mocked(api.get).mockResolvedValue({ opt_in: true, can_manage: false, eligible: true });
    renderWithProviders(<HealthOptInCard targetType="flow" targetId="f-2" />);

    const toggle = await screen.findByRole("switch", { name: "Opt in to improvement" });
    expect(toggle.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Only the flow's managers can change this.")).toBeTruthy();
  });
});
