// A time range input as a viewer meets it: a period before now, all time, or a start and an end date; and, for
// someone who may edit the dashboard, the period picked kept with it.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

import { CustomDashboardView } from "./CustomDashboardView";
import { keepInputOperation } from "./inputs";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const DEFINITION = {
  version: 2,
  title: "Board",
  visualizations: { note: { type: "xtm.text", title: "Over $time$", options: { body: "Notes" } } },
  inputs: { time: { type: "input.timerange", token: "time", title: "Period", defaultValue: "3m" } },
  layout: {
    tabs: { items: [{ layoutId: "main", label: "Main" }] },
    layoutDefinitions: { main: { structure: [{ item: "note", position: { x: 0, y: 0, w: 6, h: 4 } }] } },
    globalInputs: ["time"],
  },
};

function draw(onKeepInput?: (input: { id: string }, value: unknown) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(
    <MemoryRouter>
      <CustomDashboardView dashboard={normalizeDashboard(DEFINITION)} dashboardId={ID} revision={1} onKeepInput={onKeepInput} />
    </MemoryRouter>,
    client,
  );
}

function pick(label: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "Period" }));
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: label }));
}

describe("a time range input", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue({ id: "u1", email: "marcus@example.com" });
  });

  it("offers the periods before now, all time and a custom range", async () => {
    draw();
    expect(await screen.findByText("Over 3m")).toBeTruthy();
    fireEvent.click(screen.getByRole("combobox", { name: "Period" }));
    expect(within(screen.getByRole("listbox")).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Last 24 hours", "Last 7 days", "Last 30 days", "Last month", "Last 90 days", "Last 3 months", "Last 6 months", "Last year",
      "All time", "Custom range",
    ]); // prettier-ignore
    fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: "All time" }));
    expect(await screen.findByText("Over all")).toBeTruthy();
  });

  it("reads a custom range once it is one, never ending before it starts", async () => {
    draw();
    await screen.findByText("Over 3m");
    pick("Custom range");
    // The design system DatePicker: a day typed in the locale's pattern is committed when the field is left.
    const typeDay = (name: string, day: string) => {
      const field = screen.getByRole("textbox", { name });
      fireEvent.change(field, { target: { value: day } });
      fireEvent.blur(field);
    };
    typeDay("Start date", "04/01/2026");
    expect(await screen.findByText("Over 2026-04-01/")).toBeTruthy();
    typeDay("End date", "03/01/2026");
    expect(screen.getByText("Over 2026-04-01/")).toBeTruthy();
    typeDay("End date", "06/30/2026");
    expect(await screen.findByText("Over 2026-04-01/2026-06-30")).toBeTruthy();
  });

  it("keeps the period picked with the dashboard, for someone who may edit it", async () => {
    const keep = vi.fn();
    draw(keep);
    await screen.findByText("Over 3m");
    // The default is kept already.
    expect(screen.queryByRole("button", { name: "Keep as the dashboard's period" })).toBeNull();
    pick("Last 6 months");
    fireEvent.click(await screen.findByRole("button", { name: "Keep as the dashboard's period" }));
    expect(keep.mock.calls.map(([input, value]) => [input.id, value])).toEqual([["time", "6m"]]);
  });

  it("offers no keeping to a reader", async () => {
    draw();
    await screen.findByText("Over 3m");
    pick("Last 6 months");
    expect(await screen.findByText("Over 6m")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Keep as the dashboard's period" })).toBeNull();
  });

  it("keeps a value by writing the whole input with its new default", () => {
    expect(keepInputOperation(DEFINITION, "time", "1y")).toEqual({
      op: "set",
      path: "inputs.time",
      value: { type: "input.timerange", token: "time", title: "Period", defaultValue: "1y" },
    });
    expect(keepInputOperation(DEFINITION, "nowhere", "1y")).toBeNull();
  });
});
