/**
 * The suggested unit of work for a custom agent, accepted in one click (#9836 R8b) -
 * `yarn test:unit`. The Outcomes card offers it only to an editor of an agent that
 * counts nothing; "Use this unit" declares it per run with the existing PUT; the row
 * says which words matched; a declared or inherited agent, or a viewer who cannot
 * edit, never reads the suggestion.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { OutcomesCard } from "./OutcomesCard";
import { suggestionDeclaration } from "./roi-outcomes";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" }, isAdmin: true }) }));
vi.mock("@/components/ui/searchable-select", () => ({
  SearchableSelect: ({ id, "aria-label": label }: { id: string; "aria-label": string }) => <input id={id} aria-label={label} />,
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

const renderCard = (card: ReactElement) => renderWithProviders(<MemoryRouter>{card}</MemoryRouter>);

const SUGGESTION = {
  unit: "phishing_triaged",
  name: "Phishing email triaged",
  builtin: true,
  score: 0.667,
  matched_terms: ["phishing", "triag"],
};

function serve({ declaration = null, suggest = { agent_id: "a-1", declared: false, suggestion: SUGGESTION } }: { declaration?: unknown; suggest?: unknown } = {}) {
  get.mockImplementation(async (url: string) => {
    if (url === "/roi/units") return [{ key: "phishing_triaged", name: "Phishing email triaged", builtin: true, archived_at: null }];
    if (url.startsWith("/roi/suggest")) return suggest;
    return declaration;
  });
}

const suggestCalls = () => get.mock.calls.filter(([url]) => String(url).startsWith("/roi/suggest"));

afterEach(() => {
  get.mockReset();
  put.mockReset();
});

describe("the suggested unit on the Outcomes card", () => {
  it("offers the suggestion to an editor of an agent that counts nothing, with the matched words", async () => {
    serve();
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    expect(await screen.findByText("Suggested unit: Phishing email triaged")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Use this unit" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pick a unit" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Why this unit" })).toBeTruthy();
    expect(suggestCalls()[0][0]).toBe("/roi/suggest?agent_id=a-1");
  });

  it("declares the suggested unit per run in one click", async () => {
    serve();
    put.mockResolvedValue({});
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    fireEvent.click(await screen.findByRole("button", { name: "Use this unit" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0][0]).toBe("/roi/declarations?subject_type=agent&subject_id=a-1");
    expect(put.mock.calls[0][1]).toEqual({ unit: "phishing_triaged", count_mode: "per_run", count_path: null, units_per_count: 1 });
  });

  it("moves focus to the declared card's Edit control after accepting (UX r1 B2)", async () => {
    serve();
    put.mockImplementation(async () => {
      serve({
        declaration: {
          subject_type: "agent", subject_id: "a-1", unit: "phishing_triaged", count_mode: "per_run",
          count_path: null, units_per_count: 1, updated_at: null, origin: "custom",
        },
      });
      return {};
    });
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    fireEvent.click(await screen.findByRole("button", { name: "Use this unit" }));
    const edit = await screen.findByRole("button", { name: "Edit" });
    await waitFor(() => expect(document.activeElement).toBe(edit));
  });

  it("says so in words when saving the suggestion is refused", async () => {
    serve();
    put.mockRejectedValue(Object.assign(new Error("nope"), { status: 403 }));
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    fireEvent.click(await screen.findByRole("button", { name: "Use this unit" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "The suggested unit could not be saved. Pick a unit instead.");
  });

  it("never reads a suggestion for a viewer who cannot edit", async () => {
    serve();
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage={false} />);
    expect(await screen.findByText("Not counted in ROI. Only someone who can edit it declares a unit of work.")).toBeTruthy();
    expect(suggestCalls()).toHaveLength(0);
    expect(screen.queryByText(/Suggested unit/)).toBeNull();
  });

  it("never offers a suggestion once a unit is declared", async () => {
    serve({
      declaration: {
        subject_type: "agent", subject_id: "a-1", unit: "phishing_triaged", count_mode: "per_run",
        count_path: null, units_per_count: 1, updated_at: null, origin: "custom",
      },
    });
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    expect(await screen.findByRole("button", { name: "Edit" })).toBeTruthy();
    expect(suggestCalls()).toHaveLength(0);
  });

  it("shows nothing when the catalog has no fitting unit", async () => {
    serve({ suggest: { agent_id: "a-1", declared: false, suggestion: null } });
    renderCard(<OutcomesCard subject={{ type: "agent", id: "a-1" }} structuredOutput={false} canManage />);
    expect(await screen.findByRole("button", { name: "Pick a unit" })).toBeTruthy();
    await waitFor(() => expect(suggestCalls()).toHaveLength(1));
    expect(screen.queryByText(/Suggested unit/)).toBeNull();
  });

  it("asks nothing for a fleet (fleets declare by hand)", async () => {
    serve();
    renderCard(<OutcomesCard subject={{ type: "fleet", id: "f-1" }} structuredOutput={false} canManage />);
    expect(await screen.findByRole("button", { name: "Pick a unit" })).toBeTruthy();
    expect(suggestCalls()).toHaveLength(0);
  });
});

describe("suggestionDeclaration", () => {
  it("counts one unit per finished run", () => {
    expect(suggestionDeclaration({ unit: "cve_assessed" })).toEqual({ unit: "cve_assessed", count_mode: "per_run", count_path: null, units_per_count: 1 });
  });
});
