// Discovery, "Signals by trigger path" (CTEM-30; UX round 1 B1 on #941): the path name is
// rendered whole on its own line, never only in a native title; counts or the dialog's status follow
// (UX round 1 B1 on #1194: one status for the card and the Signal sources dialog).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { TriggerPathsCard } from "./TriggerPathsCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);

// Turn on of a profile pass is an administrator's action (UX round 1 on #1356, B3).
const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useAuth")>();
  return { ...actual, useAuth: () => ({ ...actual.useAuth(), isAdmin: auth.isAdmin }) };
});

const LONG = "Credential or secret leak tied to an in-scope identity";

beforeEach(() => {
  vi.clearAllMocks();
  auth.isAdmin = true;
  get.mockResolvedValue({
    paths: [
      {
        id: "new_exposure", label: "New exposure", default_lens: "outside", listener: true, counts: { watchlist: 1, case_opened: 1 }, configured: true,
        status: "enabled", sources: [{ provider: "opencti", kind: "listener", event: "", bound: true }],
      },
      {
        id: "credential_leak", label: LONG, default_lens: "world", listener: true, counts: {}, configured: false,
        status: "not_connected", sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }],
      },
    ],
  });
});

describe("TriggerPathsCard", () => {
  it("renders each path name whole as text, not only as a title", async () => {
    renderWithProviders(<MemoryRouter><TriggerPathsCard /></MemoryRouter>);
    const names = await screen.findAllByTestId("ctem-trigger-path-name");
    expect(names.map((n) => n.textContent)).toEqual(["New exposure", LONG]);
    expect(names.every((n) => !n.hasAttribute("title"))).toBe(true);
  });

  it("puts Not connected and Connect under a path whose source is not bound", async () => {
    renderWithProviders(<MemoryRouter><TriggerPathsCard /></MemoryRouter>);
    const rows = await screen.findAllByTestId("ctem-trigger-path");
    const leak = rows.find((r) => r.getAttribute("data-path") === "credential_leak");
    expect(leak).toBeTruthy();
    expect(within(leak as HTMLElement).getByTestId("ctem-trigger-path-status").textContent).toBe("Not connected");
    expect(within(leak as HTMLElement).getByRole("button", { name: "Connect" })).toBeTruthy();
    // No connected-count caption nor sources button any more (Sam, 23 Nov 2026, #1344).
    expect(screen.queryByText("1 of 2 trigger paths connected")).toBeNull();
    expect(screen.queryByRole("link", { name: "Signal sources" })).toBeNull();
  });

  it("reads Off with Turn on for a pass whose provider is connected (#1344)", async () => {
    get.mockResolvedValue({
      paths: [
        {
          id: "control_decay",
          label: "Control decay",
          default_lens: "inside",
          listener: false,
          counts: {},
          total: 0,
          configured: false,
          status: "off",
          sources: [{ provider: "opencrq", kind: "pass", event: "", bound: false, off: true }],
        },
      ],
    });
    renderWithProviders(<MemoryRouter><TriggerPathsCard /></MemoryRouter>);
    const row = await screen.findByTestId("ctem-trigger-path");
    expect(within(row).getByTestId("ctem-trigger-path-status").textContent).toBe("Off");
    expect(within(row).getByRole("button", { name: "Turn on" })).toBeTruthy();

    // A reader reads Off and gets no Turn on: the card it opens is an administrator's.
    auth.isAdmin = false;
    row.remove();
    renderWithProviders(<MemoryRouter><TriggerPathsCard /></MemoryRouter>);
    const readerRow = await screen.findByTestId("ctem-trigger-path");
    expect(within(readerRow).getByTestId("ctem-trigger-path-status").textContent).toBe("Off");
    expect(within(readerRow).queryByRole("button", { name: "Turn on" })).toBeNull();
  });
});
