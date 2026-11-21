// The Done for you card of the CTEM command center v2 (C2 slice V3): a
// horizontal timeline that is complete with every fleet off, a 7 / 30 day
// toggle, a cluster badge that lists its day, and a point that opens its run.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { DFY_DAY_LIST_MAX, DoneForYouCard } from "./DoneForYouCard";
import type { DfyRun } from "./done-for-you";
import type { ActivityEvent } from "./activity-feed";

const navigate = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}));
vi.mock("@/components/ui/enterprise-upsell", () => ({
  AiLockedFooter: ({ reason }: { reason: string | null }) => <p>AI locked: {String(reason)}</p>,
}));

const NOW = new Date(2026, 10, 9, 15, 0, 0).getTime();
const daysAgo = (d: number, hour = 10) => new Date(2026, 10, 9 - d, hour, 0, 0).toISOString();

function run(id: string, over: Partial<DfyRun> = {}): DfyRun {
  return {
    id, agent_name: "Exposure Briefing Analyst", assignment_name: `Briefing ${id}`, status: "completed",
    outcome: "ok", actions_taken: [], created_at: daysAgo(1), completed_at: daysAgo(1, 11), ...over,
  };
}

const synced: ActivityEvent = { kind: "posture_synced", id: "posture", at: daysAgo(0), covered: 3, total: 5 };

function card(props: Partial<Parameters<typeof DoneForYouCard>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <DoneForYouCard events={[]} runs={[]} digests={[]} fleetRuns={[]} aiLockReason={null} now={NOW} {...props} />
    </MemoryRouter>,
  );
}

describe("DoneForYouCard", () => {
  it("draws the modules and automations lanes with every fleet off, and no fleets lane", () => {
    card({ events: [synced], runs: [run("r1")] });
    const rows = screen.getAllByRole("rowheader").map((h) => h.textContent);
    expect(rows).toEqual(["Modules", "Automations"]);
    expect(screen.getByTestId("ctem-done-for-you-count").textContent).toBe("2");
    expect(screen.queryByText("Fleet run")).toBeNull();
  });

  it("says what fills it when nothing happened in the period", () => {
    card();
    expect(screen.getByText(/Nothing recorded in this period yet/)).toBeTruthy();
  });

  it("switches to 30 days and counts the older runs", () => {
    card({ runs: [run("old", { created_at: daysAgo(20) }), run("new")] });
    expect(screen.getByTestId("ctem-done-for-you-count").textContent).toBe("1");
    fireEvent.click(within(screen.getByTestId("ctem-done-for-you-period")).getByRole("radio", { name: "30 days" }));
    expect(screen.getByTestId("ctem-done-for-you-count").textContent).toBe("2");
  });

  it("opens the run of a point", () => {
    navigate.mockClear();
    card({ runs: [run("r1")] });
    fireEvent.click(screen.getByRole("button", { name: /^Briefing r1,/ }));
    expect(navigate).toHaveBeenCalledWith("/operations/executions/r1", expect.anything());
  });

  it("clusters a busy day and lists it on click", () => {
    const runs = ["a", "b", "c", "d", "e"].map((id, i) => run(id, { created_at: daysAgo(1, 8 + i) }));
    card({ runs });
    const badge = screen.getByTestId("ctem-done-for-you-cluster");
    expect(badge.textContent).toBe("5");
    fireEvent.click(badge);
    const day = screen.getByTestId("ctem-done-for-you-day");
    expect(within(day).getAllByRole("listitem")).toHaveLength(5);
    expect(within(day).getAllByRole("button", { name: /^Open run: / })).toHaveLength(5);
    fireEvent.click(within(day).getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("ctem-done-for-you-day")).toBeNull();
  });

  it("caps a busy day list and sends the rest to Executions, so the card never scrolls inside itself", () => {
    navigate.mockClear();
    const runs = Array.from({ length: 30 }, (_, i) => run(`b${i}`, { created_at: new Date(2026, 10, 8, 0, i * 2).toISOString() }));
    card({ runs });
    fireEvent.click(screen.getByTestId("ctem-done-for-you-cluster"));
    const day = screen.getByTestId("ctem-done-for-you-day");
    expect(within(day).getAllByRole("listitem")).toHaveLength(DFY_DAY_LIST_MAX);
    const more = within(day).getByRole("button", { name: `${30 - DFY_DAY_LIST_MAX} more in Executions` });
    fireEvent.click(more);
    expect(navigate).toHaveBeenCalledWith("/operations/executions");
  });

  it("marks a count badge that holds a failed run as needing attention, else neutral", () => {
    const ok = ["a", "b", "c", "d"].map((id, i) => run(id, { created_at: daysAgo(1, 8 + i) }));
    const mixed = ["e", "f", "g"].map((id, i) => run(id, { created_at: daysAgo(2, 8 + i) }));
    mixed.push(run("h", { created_at: daysAgo(2, 12), outcome: "failed", status: "failed" }));
    card({ runs: [...ok, ...mixed] });
    const tones = screen.getAllByTestId("ctem-done-for-you-cluster").map((b) => b.dataset.tone);
    expect(tones.sort()).toEqual(["attention", "neutral"]);
  });

  it("draws every 30 day tick whole, never truncated", () => {
    card();
    fireEvent.click(within(screen.getByTestId("ctem-done-for-you-period")).getByRole("radio", { name: "30 days" }));
    const ticks = screen.getAllByTestId("ctem-done-for-you-tick").map((t) => t.textContent);
    expect(ticks).toHaveLength(5);
    expect(ticks[ticks.length - 1]).toBe("Today");
    expect(ticks.every((t) => t && !t.endsWith("…"))).toBe(true);
  });

  it("puts the tick of each day in the same grid column as that day's lane cell", () => {
    card({ runs: [run("r1")] });
    const [header, ...lanes] = screen.getAllByRole("row");
    const headerCells = Array.from(header.children);
    // The lane header is an in-flow grid item that only wraps the visually hidden text.
    const laneHeader = screen.getByTestId("ctem-done-for-you-lane-header");
    expect(headerCells[0]).toBe(laneHeader);
    expect(laneHeader.firstElementChild?.textContent).toBe("Lane");
    for (const lane of lanes) {
      const cells = Array.from(lane.children);
      expect(cells).toHaveLength(headerCells.length);
      expect(cells[0].getAttribute("role")).toBe("rowheader");
      cells.slice(1).forEach((cell, day) => {
        expect(cell.getAttribute("role")).toBe("cell");
        expect(headerCells[day + 1].getAttribute("role")).toBe("columnheader");
      });
    }
    const days = headerCells.slice(1).map((h) => h.getAttribute("aria-label") ?? "");
    expect(days[days.length - 1]).toMatch(/9/);
  });

  it("adds the outcomes lane when a run recorded actions", () => {
    card({ runs: [run("r1", { actions_taken: [{}, {}] })] });
    expect(screen.getAllByRole("rowheader").map((h) => h.textContent)).toEqual(["Modules", "Automations", "Outcomes"]);
  });

  it("reports the height of its content", () => {
    const heights: number[] = [];
    card({ onContentHeight: (px) => heights.push(px) });
    expect(heights.length).toBeGreaterThan(0);
  });

  it("keeps the one way forward when AI is locked", () => {
    card({ aiLockReason: "ai_off", events: [synced] });
    expect(screen.getByText("AI locked: ai_off")).toBeTruthy();
    expect(screen.getAllByRole("rowheader")).toHaveLength(2);
  });
});
