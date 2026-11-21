// Discovery, "Signals by trigger path" (CTEM-30; UX round 1 B1 on #941): the path name is
// rendered whole on its own line, never only in a native title; counts or "Not configured" follow.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { TriggerPathsCard } from "./TriggerPathsCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);

const LONG = "Credential or secret leak tied to an in-scope identity";

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({
    paths: [
      { id: "new_exposure", label: "New exposure", default_lens: "outside", listener: true, counts: { watchlist: 1, case_opened: 1 }, configured: true },
      { id: "credential_leak", label: LONG, default_lens: "world", listener: false, counts: {}, configured: false },
    ],
  });
});

describe("TriggerPathsCard", () => {
  it("renders each path name whole as text, not only as a title", async () => {
    renderWithProviders(<TriggerPathsCard onConnect={() => {}} />);
    const names = await screen.findAllByTestId("ctem-trigger-path-name");
    expect(names.map((n) => n.textContent)).toEqual(["New exposure", LONG]);
    expect(names.every((n) => !n.hasAttribute("title"))).toBe(true);
  });

  it("puts Not configured and Connect under a path with no source", async () => {
    renderWithProviders(<TriggerPathsCard onConnect={() => {}} />);
    const rows = await screen.findAllByTestId("ctem-trigger-path");
    const leak = rows.find((r) => r.getAttribute("data-path") === "credential_leak");
    expect(leak).toBeTruthy();
    expect(within(leak as HTMLElement).getByTestId("ctem-trigger-path-not-configured").textContent).toBe("Not configured");
    expect(within(leak as HTMLElement).getByRole("button", { name: "Connect" })).toBeTruthy();
  });
});
