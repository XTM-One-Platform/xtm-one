// useHealthName (#8261): built in since #101 (S1), so the name is asked
// with no feature flag; a caller may still hold it back with `enabled`, and a
// failed read keeps the default name instead of reaching the error boundary
// (the app runs its queries with throwOnError).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { useHealthName } from "./useHealthApi";

function Name({ enabled }: { enabled?: boolean }) {
  return <p data-testid="name">{useHealthName(enabled)}</p>;
}

describe("useHealthName", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("asks nothing while the caller holds it back", () => {
    renderWithProviders(<Name enabled={false} />);
    expect(screen.getByTestId("name")).toHaveTextContent("Platform Health");
    expect(api.get).not.toHaveBeenCalled();
  });

  it("asks with no feature flag and keeps the default name when the read fails", async () => {
    api.get.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    renderWithProviders(<Name />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/health/name"));
    expect(screen.getByTestId("name")).toHaveTextContent("Platform Health");
  });

  it("shows the server's name once read", async () => {
    api.get.mockResolvedValue({ name: "Jeeves", default_name: "Platform Health" });
    renderWithProviders(<Name />);
    expect(await screen.findByText("Jeeves")).toBeInTheDocument();
  });
});
