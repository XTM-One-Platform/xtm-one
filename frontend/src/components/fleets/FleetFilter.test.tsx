// The "All fleets" picker shows only for a reader who can access a fleet.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { FleetFilter } from "./FleetFilter";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FleetFilter", () => {
  it("renders nothing for a reader with no accessible fleet, even when the URL names one", async () => {
    get.mockResolvedValue([]);
    const { container } = renderWithProviders(<FleetFilter value="f-gone" onChange={vi.fn()} />);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/fleets"));
    await waitFor(() => expect(container.textContent).toBe(""));
    expect(screen.queryByLabelText("Filter by fleet")).toBeNull();
  });

  it("renders nothing when the fleet list is refused", async () => {
    get.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
    const { container } = renderWithProviders(<FleetFilter value={undefined} onChange={vi.fn()} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("renders the picker for a reader with a company-managed fleet", async () => {
    get.mockResolvedValue([{ id: "f-ceo", name: "CEO Office", is_company_managed: true, user_id: null }]);
    renderWithProviders(<FleetFilter value={undefined} onChange={vi.fn()} />);
    expect(await screen.findByLabelText("Filter by fleet")).toBeTruthy();
    expect(screen.getByText("All fleets")).toBeTruthy();
  });
});
