// The source editor: the whole definition is written with PUT /definition, an
// element with a `set` operation, both at the revision read; text that is not
// JSON is never sent, and the server's reason stays on screen with the text.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), patch: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardSourceDialog } from "./DashboardSourceDialog";

const SPEC = {
  version: 2,
  title: "Threat board",
  dataSources: { answer: { type: "static", options: { shape: "number", data: { value: 42 } } } },
  visualizations: { big: { type: "xtm.singlevalue", title: "Answer", dataSources: { primary: "answer" } } },
  layout: {
    tabs: { items: [{ layoutId: "layout_1", label: "Overview" }] },
    layoutDefinitions: { layout_1: { structure: [{ item: "big", position: { x: 0, y: 0, w: 4, h: 6 } }] } },
  },
};
const DASHBOARD = { id: "d1", title: "Threat board", revision: 3, can_manage: true, spec: SPEC, widgets: [] } as unknown as CustomDashboard;

function open(selectedId: string | null = null, dashboard = DASHBOARD) {
  renderWithProviders(<DashboardSourceDialog dashboard={dashboard} selectedId={selectedId} open onOpenChange={() => {}} />);
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

describe("DashboardSourceDialog", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("writes the whole definition at the revision read", async () => {
    api.put.mockResolvedValue({ ...DASHBOARD, revision: 4 });
    const editor = open();
    expect(JSON.parse(editor.value)).toEqual(SPEC);
    fireEvent.change(editor, { target: { value: JSON.stringify({ ...SPEC, title: "Renamed" }) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.put.mock.calls).toEqual([
        ["/custom-dashboards/d1/definition", { revision: 3, definition: { ...SPEC, title: "Renamed" } }],
      ]),
    );
  });

  it("shows and writes the whole dashboard as its stored manifest when the server sends one", async () => {
    const manifest = {
      config: { relativeDate: null, startDate: null, endDate: null },
      variables: [],
      widgets: { big: { id: "big", type: "x_xtm_one.singlevalue", perspective: null, dataSelection: [], parameters: { title: "Answer" } } },
      x_xtm_one: { version: 3, title: "Threat board" },
    };
    api.put.mockResolvedValue({ ...DASHBOARD, revision: 4 });
    const editor = open(null, { ...DASHBOARD, manifest } as CustomDashboard);
    expect(JSON.parse(editor.value)).toEqual(manifest);
    const renamed = { ...manifest, x_xtm_one: { ...manifest.x_xtm_one, title: "Renamed" } };
    fireEvent.change(editor, { target: { value: JSON.stringify(renamed) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.put.mock.calls).toEqual([["/custom-dashboards/d1/definition", { revision: 3, definition: renamed }]]),
    );
  });

  it("writes the picked widget's visualization alone as a set operation", async () => {
    api.patch.mockResolvedValue({ ...DASHBOARD, revision: 4 });
    open("big");
    expect(screen.getByRole("button", { name: "Data source · answer" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Visualization · big" }));
    const editor = screen.getByRole("textbox") as HTMLTextAreaElement;
    await waitFor(() => expect(JSON.parse(editor.value)).toEqual(SPEC.visualizations.big));
    const value = { ...SPEC.visualizations.big, title: "The answer" };
    fireEvent.change(editor, { target: { value: JSON.stringify(value) } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.patch.mock.calls).toEqual([
        ["/custom-dashboards/d1", { revision: 3, operations: [{ op: "set", path: "visualizations.big", value }] }],
      ]),
    );
  });

  it("never sends text that is not JSON", () => {
    const editor = open();
    fireEvent.change(editor, { target: { value: "{title: 'x'}" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert").textContent).toMatch(/^This is not valid JSON: /);
    expect(api.put.mock.calls).toEqual([]);
  });

  it("keeps the text and shows the server's reason when the definition is refused", async () => {
    api.put.mockRejectedValue(
      Object.assign(new Error("visualizations.big.dataSources.primary names no data source 'ghost'"), { status: 422 }),
    );
    const editor = open();
    const text = JSON.stringify({ ...SPEC, visualizations: { big: { type: "xtm.singlevalue", dataSources: { primary: "ghost" } } } });
    fireEvent.change(editor, { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "visualizations.big.dataSources.primary names no data source 'ghost'",
    );
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(text);
  });

  it("shows the source read-only to someone who may not edit", () => {
    const editor = open(null, { ...DASHBOARD, can_manage: false });
    expect(editor.disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
