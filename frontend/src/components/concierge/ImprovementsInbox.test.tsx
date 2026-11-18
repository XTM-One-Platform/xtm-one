// The inbox is a server-mode data table (#9378, slice 6): it pages by the
// server's total, sends the search, the Type and Status pills and the sort to
// the API, and opens a row in place on the proposal.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { ImprovementsInbox } from "./ImprovementsInbox";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

function page(path: string) {
  const query = new URLSearchParams(path.split("?")[1]);
  const offset = Number(query.get("offset") ?? 0);
  const limit = Number(query.get("limit") ?? 25);
  if (!path.startsWith("/health/improvements?")) return Promise.resolve([]);
  return Promise.resolve({
    total: 45,
    items: Array.from({ length: Math.min(limit, 45 - offset) }, (_, i) => ({
      id: `p-${offset + i}`,
      proposal_type: "persona",
      target_type: "agent",
      target_id: "a-1",
      agent_id: "a-1",
      status: "pending_approval",
      summary: `Proposal ${offset + i}`,
      experiment_verdict: null,
      experiment: {},
      can_decide: i === 0,
      created_at: "2026-11-01T00:00:00Z",
    })),
  });
}

function render(initial = "/") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[initial]}>
      <ImprovementsInbox fixed={{ agent_id: "a-1" }} />
    </MemoryRouter>,
  );
}

const calls = () => vi.mocked(api.get).mock.calls.map(([path]) => String(path));

describe("the Improvements inbox", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockImplementation((path: string) => page(path));
  });

  it("asks the server for the next page and says how many there are", async () => {
    render();
    expect((await screen.findAllByText("Page 1 of 2")).length).toBeGreaterThan(0);
    expect(calls()).toContain("/health/improvements?agent_id=a-1&sort=date&direction=desc&limit=25&offset=0");

    fireEvent.click(screen.getAllByRole("button", { name: "Next page" })[0]);

    await waitFor(() =>
      expect(calls()).toContain("/health/improvements?agent_id=a-1&sort=date&direction=desc&limit=25&offset=25"),
    );
    expect((await screen.findAllByText("Page 2 of 2")).length).toBeGreaterThan(0);
  });

  it("sends the URL's search, filters and sort to the API", async () => {
    render("/?imp_q=tone&imp_f_type=persona&imp_f_type=skill&imp_f_status=kept&imp_sort=status:asc");
    await screen.findAllByTestId("improvement-row");
    expect(calls()).toContain(
      "/health/improvements?agent_id=a-1&proposal_type=persona&proposal_type=skill&status=kept&q=tone&sort=status&direction=asc&limit=25&offset=0",
    );
  });

  it("draws one line per proposal with its type, verdict and status, and opens it in place", async () => {
    render();
    const rows = await screen.findAllByTestId("improvement-row");
    expect(rows).toHaveLength(25);
    expect(screen.getAllByText("Unmeasured").length).toBe(25);
    expect(screen.getByText("Awaiting your decision")).toBeTruthy();
    expect(screen.getByTitle("Proposal 0")).toBeTruthy();

    const open = screen.getAllByRole("button", { name: "Expand" })[0];
    expect(open.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(open);
    expect(screen.getByRole("button", { name: "Collapse" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Open the target")).toBeTruthy();
  });

  it("shows the decision badge alone in Status, never beside a cut status (UX round 1 on #10399, B1)", async () => {
    render();
    const rows = await screen.findAllByTestId("improvement-row");
    // Row 0 is the viewer's to decide: the badge alone; the others say their status.
    expect(within(rows[0]).getByText("Awaiting your decision")).toBeTruthy();
    expect(within(rows[0]).queryByText("Awaiting approval")).toBeNull();
    expect(within(rows[1]).getByText("Awaiting approval")).toBeTruthy();
    expect(within(rows[1]).queryByText("Awaiting your decision")).toBeNull();
  });

  it("draws Type, Verdict and Status as cut badges with a Tip, never a native title (UX round 2 on #10399, B1, B2)", async () => {
    render();
    const rows = await screen.findAllByTestId("improvement-row");
    for (const row of rows.slice(0, 2)) {
      const badges = within(row).getAllByTestId("improvement-cell-badge");
      // One badge per drawn Type, Verdict and Status cell: every row is the same height.
      expect(badges.length).toBe(3);
      for (const badge of badges) {
        expect(badge.getAttribute("title")).toBeNull();
        expect(badge.querySelector("span.truncate")).not.toBeNull();
      }
    }
    expect(within(rows[1]).getByText("Awaiting approval").closest("[data-testid='improvement-cell-badge']")).not.toBeNull();
  });

  describe.each([
    { width: 384, shown: ["When", "Summary"], hidden: ["Type", "Verdict", "Status"] },
    { width: 728, shown: ["When", "Status", "Summary"], hidden: ["Type", "Verdict"] },
    { width: 1200, shown: ["When", "Type", "Verdict", "Status", "Summary"], hidden: [] },
  ])("in a $width px box (UX round 1 on #10399, B2)", ({ width, shown, hidden }) => {
    const original = globalThis.ResizeObserver;
    let spy: { mockRestore: () => void } | undefined;
    beforeEach(() => {
      spy = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(width);
      globalThis.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver;
      return () => {
        spy?.mockRestore();
        globalThis.ResizeObserver = original;
      };
    });

    it("keeps Summary wide by stepping columns aside, and the opened row says them", async () => {
      render();
      await screen.findAllByTestId("improvement-row");
      for (const name of shown) expect(screen.getByRole("columnheader", { name })).toBeTruthy();
      for (const name of hidden) expect(screen.queryByRole("columnheader", { name })).toBeNull();
      // The fixed widths drawn leave Summary at least 200 px of the box.
      const fixed = { When: 144, Type: 160, Verdict: 176, Status: 224 } as Record<string, number>;
      const taken = shown.reduce((sum, name) => sum + (fixed[name] ?? 0), 0);
      expect(width - taken).toBeGreaterThanOrEqual(200);

      fireEvent.click(screen.getAllByRole("button", { name: "Expand" })[1]);
      const facts = screen.getByTestId("improvement-facts");
      expect(within(facts).getByText("Persona")).toBeTruthy();
      expect(within(facts).getByText("Unmeasured")).toBeTruthy();
      expect(within(facts).getByText("Awaiting approval")).toBeTruthy();
    });
  });
});
