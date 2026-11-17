// useConciergeName (#8261): asked only while CONCIERGE is on, and a failed
// read keeps the default name instead of reaching the error boundary (the
// app runs its queries with throwOnError).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn() }));
const flags = vi.hoisted(() => ({ concierge: false }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => flags.concierge }));

import { useConciergeName } from "./useConciergeApi";

function Name() {
  return <p data-testid="name">{useConciergeName()}</p>;
}

describe("useConciergeName", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.concierge = false;
  });

  it("asks nothing while the flag is off", () => {
    renderWithProviders(<Name />);
    expect(screen.getByTestId("name")).toHaveTextContent("Concierge");
    expect(api.get).not.toHaveBeenCalled();
  });

  it("keeps the default name when the read fails", async () => {
    flags.concierge = true;
    api.get.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    renderWithProviders(<Name />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/concierge/name"));
    expect(screen.getByTestId("name")).toHaveTextContent("Concierge");
  });

  it("shows the server's name once read", async () => {
    flags.concierge = true;
    api.get.mockResolvedValue({ name: "Jeeves", default_name: "Concierge" });
    renderWithProviders(<Name />);
    expect(await screen.findByText("Jeeves")).toBeInTheDocument();
  });
});
