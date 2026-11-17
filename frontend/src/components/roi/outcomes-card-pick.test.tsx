/**
 * The Outcomes card opened from "Pick a unit" (UX #9810 B2) - `yarn test:unit`.
 * The agent Overview's ROI sends to Configuration > Outcomes with `?pick=unit`:
 * without the flag, or for a viewer who cannot edit, the card stays on its
 * not-counted line; with it, an editor lands on the unit picker, focused.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { OutcomesCard } from "./OutcomesCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" }, isAdmin: true }) }));
// The picker has its own tests; here only its id and its focus matter.
vi.mock("@/components/ui/searchable-select", () => ({
  SearchableSelect: ({ id, "aria-label": label }: { id: string; "aria-label": string }) => <input id={id} aria-label={label} />,
}));

const get = vi.mocked(api.get);

// The editing form links to Settings > Units for an administrator: it needs a router.
const renderCard = (card: ReactElement) => renderWithProviders(<MemoryRouter>{card}</MemoryRouter>);

function serveNothingDeclared() {
  get.mockImplementation(async (url: string) => (url === "/roi/units" ? [] : null));
}

afterEach(() => {
  get.mockReset();
});

describe("the Outcomes card opened from Pick a unit", () => {
  it("opens on the unit picker, focused, when nothing is declared", async () => {
    serveNothingDeclared();
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage pickOnOpen />);
    const picker = await screen.findByRole("textbox", { name: "Unit of work" });
    await waitFor(() => expect(document.activeElement).toBe(picker));
    expect(screen.queryByRole("button", { name: "Pick a unit" })).toBeNull();
  });

  it("stays on its not-counted line without the flag", async () => {
    serveNothingDeclared();
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    expect(await screen.findByRole("button", { name: "Pick a unit" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Unit of work" })).toBeNull();
  });

  it("never opens the picker for a viewer who cannot edit", async () => {
    serveNothingDeclared();
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage={false} pickOnOpen />);
    expect(await screen.findByText("Not counted in ROI. Only someone who can edit it declares a unit of work.")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Unit of work" })).toBeNull();
  });

  it("renders a refused declaration (403) as the read-only state, never as a read error (UX #9810 r3 B1)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url === "/roi/units") return [];
      throw Object.assign(new Error("You cannot manage this agent."), { status: 403 });
    });
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage pickOnOpen />);
    expect(await screen.findByText("Not counted in ROI. Only someone who can edit it declares a unit of work.")).toBeTruthy();
    expect(screen.queryByText("The outcomes declaration could not be read.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Pick a unit" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Unit of work" })).toBeNull();
  });
});

describe("a built-in agent's declaration (#9914 UX round 1)", () => {
  const shipped = {
    subject_type: "agent",
    subject_id: "a-1",
    unit: "cve_assessed",
    count_mode: "per_output_item",
    count_path: "findings",
    units_per_count: 1,
    updated_at: null,
    origin: "default",
    customized_by: null,
    customized_at: null,
    default: { unit: "cve_assessed", count_mode: "per_output_item", count_path: "findings", units_per_count: 1 },
  };

  it("shows the shipped declaration as Default, customizable, with nothing to reset (B2)", async () => {
    get.mockImplementation(async (url: string) => (url === "/roi/units" ? [] : shipped));
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput canManage />);
    expect(await screen.findByRole("button", { name: "Customize" })).toBeTruthy();
    expect(screen.queryByText(/Not counted in ROI/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Reset to default" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop counting" })).toBeNull();
  });

  it("offers Reset to default on a customized one (B1)", async () => {
    const customized = { ...shipped, unit: "ttp_extraction", origin: "customized", customized_by: "Ada Admin" };
    get.mockImplementation(async (url: string) => (url === "/roi/units" ? [] : customized));
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput canManage />);
    expect(await screen.findByRole("button", { name: "Reset to default" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("names the inherited action Customize, never Override (B5)", async () => {
    const fleet = { ...shipped, subject_type: "fleet", subject_id: "f-1", origin: "custom", default: null };
    get.mockImplementation(async (url: string) => {
      if (url === "/roi/units") return [];
      return url.includes("subject_type=fleet") ? fleet : null;
    });
    renderCard(
      <OutcomesCard
        subject={{ type: "agent", id: "a-1" }}
        parents={[{ subject: { type: "fleet", id: "f-1" }, kind: "fleet", name: "Posture" }]}
        structuredOutput
        canManage
      />,
    );
    expect(await screen.findByRole("button", { name: "Customize" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Override" })).toBeNull();
  });
});
