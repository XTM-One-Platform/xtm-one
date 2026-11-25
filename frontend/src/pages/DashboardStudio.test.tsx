// The Dashboard Studio page: a click picks a widget, the inspector edits its
// heading and saves the stored widget as an `upsert` at the revision read (a
// widget is placed and sized on the grid, `CustomDashboardView.test.tsx`); a
// reader gets the stage and the inspector read-only, without the rail.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
// The chat rail has its own tests: here it says what it was told.
vi.mock("@/components/custom-dashboard/DashboardStudioRail", () => ({
  DashboardStudioRail: ({
    focusedWidget,
    autoSendMessage,
    initialAgentId,
    onSaved,
  }: {
    focusedWidget: { id: string } | null;
    autoSendMessage?: string | null;
    initialAgentId?: string | null;
    onSaved?: (revision: number, focusWidgetId: string | null) => void;
  }) => (
    <>
      <div data-testid="rail">rail:{focusedWidget?.id ?? "none"}</div>
      <div data-testid="rail-launch">{JSON.stringify({ send: autoSendMessage ?? null, agent: initialAgentId })}</div>
      <button type="button" onClick={() => onSaved?.(4, "note")}>
        agent saved note
      </button>
    </>
  ),
  DashboardStudioHandle: () => <div data-testid="handle" />,
}));

import DashboardStudio from "./DashboardStudio";

const ID = "11111111-1111-4111-8111-111111111111";
const NOTE = { kind: "text", id: "note", title: "Notes", body: "Hello", width: "full" };

const WALL = {
  version: 2,
  title: "Threat board",
  visualizations: { box: { type: "xtm.shape.rectangle", options: {} } },
  layout: {
    tabs: { items: [{ layoutId: "wall", label: "Wall" }] },
    layoutDefinitions: { wall: { type: "absolute", options: { width: 800, height: 400 }, structure: [{ item: "box", type: "block", position: { x: 0, y: 0, w: 10, h: 10 } }] } },
  },
};

function serve(canManage: boolean, spec: object = { title: "Threat board", widgets: [NOTE] }) {
  const dashboard = {
    id: ID, title: "Threat board", widget_count: 1, revision: 4, created_at: null, updated_at: null,
    assignment_run_id: null, conversation_id: null, can_manage: canManage, can_use_as_home: true,
    permission: canManage ? "owner" : "read_only", is_company_managed: false, is_shared: false, user_id: "u1",
    shared_user_ids: [], shared_user_permissions: {}, shared_group_ids: [], shared_group_permissions: {},
    access_restricted: false, spec, widgets: [NOTE],
  };
  api.get.mockImplementation(async () => dashboard);
  return dashboard;
}

function draw(state?: object) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[{ pathname: `/dashboards/${ID}/studio`, state }]}>
      <Routes>
        <Route path="/dashboards/:id/studio" element={<DashboardStudio />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("DashboardStudio", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("hands the rail the create dialog's brief and agent, once", async () => {
    serve(true);
    draw({ send: "Build this dashboard:\n\nCritical detections", agentId: "ag1" });
    await waitFor(() =>
      expect(JSON.parse(screen.getByTestId("rail-launch").textContent ?? "")).toEqual({
        send: "Build this dashboard:\n\nCritical detections",
        agent: "ag1",
      }),
    );
  });

  it("follows the agent: the widget a dashboard tool saved is selected", async () => {
    serve(true);
    draw();
    expect(await screen.findByText("Hello")).toBeTruthy();
    expect(screen.getByTestId("rail").textContent).toBe("rail:none");
    fireEvent.click(screen.getByRole("button", { name: "agent saved note" }));
    await waitFor(() => expect(screen.getByTestId("rail").textContent).toBe("rail:note"));
  });

  it("picks a widget, tells the rail, and saves its heading as an upsert", async () => {
    serve(true);
    api.patch.mockResolvedValue({});
    draw();
    expect(await screen.findByText("Pick a widget on the page to see and change it.")).toBeTruthy();
    fireEvent.click(screen.getByText("Hello"));
    expect(screen.getByTestId("rail").textContent).toBe("rail:note");
    fireEvent.change(screen.getByLabelText("Heading"), { target: { value: "Analyst notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save widget" }));
    await waitFor(() =>
      expect(api.patch.mock.calls).toEqual([
        [
          `/custom-dashboards/${ID}`,
          { revision: 4, operations: [{ op: "upsert", widget: { ...NOTE, title: "Analyst notes" } }] },
        ],
      ]),
    );
  });

  it("gives a reader the stage, read-only, and no rail", async () => {
    serve(false);
    draw();
    fireEvent.click(await screen.findByText("Hello"));
    expect(screen.queryByTestId("rail")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save widget" })).toBeNull();
    expect((screen.getByLabelText("Heading") as HTMLInputElement).disabled).toBe(true);
  });

  it("adds an element to a canvas tab in Edit mode and saves the difference; View mode has no design tools", async () => {
    const dashboard = serve(true, WALL);
    api.patch.mockImplementation(async (_url: string, body: { revision: number }) => ({ ...dashboard, revision: body.revision + 1 }));
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Add a rectangle" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    const [url, body] = api.patch.mock.calls[0];
    expect(url).toBe(`/custom-dashboards/${ID}`);
    expect(body.revision).toBe(4);
    expect(body.operations.map((op: { op: string; path: string }) => `${op.op} ${op.path}`)).toEqual(["set visualizations.rectangle", "set layout"]);
    expect(body.operations[0].value).toEqual({ type: "xtm.shape.rectangle", options: { fillOpacity: 0.15, strokeWidth: 2, rx: 8 } });
    const edit = screen.getByRole("button", { name: "Edit the dashboard" });
    expect(edit.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(edit);
    expect(edit.getAttribute("aria-pressed")).toBe("false");
    expect(screen.queryByRole("toolbar", { name: "Design" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a tab" })).toBeNull();
  });

  it("folds the settings panel away for a wider canvas, and unfolds it when a tool asks for it", async () => {
    const dashboard = serve(true, WALL);
    api.patch.mockImplementation(async (_url: string, body: { revision: number }) => ({ ...dashboard, revision: body.revision + 1 }));
    draw();
    // The panel's switch is in More actions, as the Video Studio keeps its secondary actions.
    const more = async () => {
      fireEvent.pointerDown(await screen.findByRole("button", { name: "More actions" }), { button: 0, ctrlKey: false });
      return screen.findAllByRole("menuitem");
    };
    fireEvent.click((await more()).find((item) => item.textContent === "Hide the settings panel")!);
    await waitFor(() => expect(screen.queryByRole("menuitem")).toBeNull());
    expect((await more()).map((item) => item.textContent)).toContain("Show the settings panel");
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menuitem")).toBeNull());
    // Adding an element opens its settings.
    fireEvent.click(screen.getByRole("button", { name: "Add a rectangle" }));
    await waitFor(async () => expect((await more()).map((item) => item.textContent)).toContain("Hide the settings panel"));
  });

  it("gives a reader no mode switch and no design tools", async () => {
    serve(false, WALL);
    draw();
    expect(await screen.findByTestId("dashboard.canvas")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit the dashboard" })).toBeNull();
    expect(screen.queryByRole("toolbar", { name: "Design" })).toBeNull();
  });
});
