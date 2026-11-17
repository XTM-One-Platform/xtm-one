// The studio's interactions and visibility editor: an interaction added or
// removed is one `set` of the visualization; the visibility writes its own
// conditions and the visualization naming them.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn().mockResolvedValue([]) }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { VisualizationInteractions } from "./VisualizationInteractions";

const VIZ = {
  type: "xtm.table",
  dataSources: { primary: "hosts" },
  eventHandlers: [{ type: "drilldown.switchTab", options: { layoutId: "two" } }],
};
const SPEC = { version: 2, title: "T", visualizations: { table: VIZ }, expressions: {}, layout: { tabs: { items: [] } } };
const DASHBOARD = { id: "d1", revision: 2, can_manage: true, spec: SPEC, widgets: [] } as unknown as CustomDashboard;

function open(edit = vi.fn()) {
  renderWithProviders(<VisualizationInteractions dashboard={DASHBOARD} vizId="table" edit={edit} />);
  return edit;
}

describe("VisualizationInteractions", () => {
  it("adds an interaction setting a token from the clicked value", () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "Add an interaction" }));
    const form = screen.getByTestId("dashboard.interactions.new");
    fireEvent.change(within(form).getByRole("textbox", { name: "Token" }), { target: { value: "host" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add the interaction" }));
    expect(edit.mock.calls[0][0]).toEqual([
      {
        op: "set",
        path: "visualizations.table",
        value: { ...VIZ, eventHandlers: [...VIZ.eventHandlers, { type: "drilldown.setToken", options: { tokens: [{ token: "host", key: "value" }] } }] },
      },
    ]);
  });

  it("removes an interaction", () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "Remove the interaction" }));
    expect(edit.mock.calls[0][0]).toEqual([{ op: "set", path: "visualizations.table", value: { ...VIZ, eventHandlers: [] } }]);
  });

  it("saves the visibility as the visualization's own conditions", () => {
    const edit = open();
    fireEvent.change(screen.getByRole("textbox", { name: "Show only when" }), { target: { value: "isSet($host$)" } });
    fireEvent.click(screen.getByRole("switch", { name: "Hide when there is no data" }));
    fireEvent.click(screen.getByRole("button", { name: "Save the visibility" }));
    expect(edit.mock.calls[0][0]).toEqual([
      { op: "set", path: "expressions", value: { conditions: { show_table: { name: "Show table", value: "isSet($host$)" } } } },
      {
        op: "set",
        path: "visualizations.table",
        value: { ...VIZ, containerOptions: { visibility: { showConditions: ["show_table"], hideConditions: [], hideWhenNoData: true } } },
      },
    ]);
  });
});
