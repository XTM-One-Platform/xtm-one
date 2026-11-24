// Settings > Organization: "Use organization context in every agent" (#11533), read view at rest, the switch on Edit.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { OrganizationContextInjectionCard } from "./OrganizationContextInjectionCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/organization-context/injection")
      return { enabled: true, categories: [], available_categories: ["primary_assets", "software"] };
    throw new Error(`unexpected GET ${path}`);
  });
  put.mockImplementation(async (_path: string, body: unknown) => body);
});

describe("OrganizationContextInjectionCard", () => {
  it("reads On at rest, never a disabled switch", async () => {
    renderWithProviders(<OrganizationContextInjectionCard />);
    const card = await screen.findByTestId("settings.organization.injection");
    await waitFor(() => expect(card.textContent).toContain("On"));
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("reads the preloaded categories at rest, None by default", async () => {
    renderWithProviders(<OrganizationContextInjectionCard />);
    const card = await screen.findByTestId("settings.organization.injection");
    await waitFor(() => expect(card.textContent).toContain("Preloaded organization categories"));
    expect(card.textContent).toContain("None");
  });

  it("turns it off for every agent that inherits on Save", async () => {
    renderWithProviders(<OrganizationContextInjectionCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit organization context in agents" }));
    fireEvent.click(await screen.findByRole("switch", { name: /Use organization context in every agent/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/organization-context/injection", { enabled: false, categories: [] }),
    );
    await waitFor(() => expect(screen.queryByRole("switch")).toBeNull());
  });
});
