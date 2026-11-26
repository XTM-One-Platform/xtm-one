// The dashboards list: with the Dashboard Studio, a dashboard is created empty or imported from a file, then
// opened in the studio; without it, the list offers neither.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), postForm: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
const flags = vi.hoisted(() => ({ studio: true }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => flags.studio }));

import CustomDashboards from "./CustomDashboards";

const ID = "11111111-1111-4111-8111-111111111111";

const board = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
  id, title, widget_count: 2, revision: 1, conversation_id: null, assignment_run_id: null, created_at: null,
  updated_at: "2026-10-20T10:00:00Z", user_id: "u1", is_company_managed: false, is_shared: false, permission: "owner", tags: [],
  ...extra,
}); // prettier-ignore
const BOARDS = [
  board("a", "Weekly review", { tags: ["soc"], updated_at: "2026-10-25T10:00:00Z" }),
  board("b", "Exposure", { tags: ["ctem", "soc"] }),
  board("c", "Their board", { user_id: "u2", is_shared: true, permission: "read_only", updated_at: "2026-10-24T10:00:00Z" }),
];

/** The studio route: what the create dialog handed it (`DashboardStudioLaunch`). */
function Studio() {
  const { state } = useLocation();
  return (
    <>
      <p>studio of the new dashboard</p>
      <pre data-testid="launch">{JSON.stringify(state ?? {})}</pre>
    </>
  );
}

function draw(path = "/dashboards") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/dashboards" element={<CustomDashboards />} />
        <Route path="/dashboards/:id/studio" element={<Studio />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CustomDashboards", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
    flags.studio = true;
    window.localStorage.clear();
    api.get.mockImplementation(async (path: string) => (path === "/auth/me" ? { id: "u1", home_dashboard_id: null } : []));
  });

  it("creates an empty dashboard from its name and description, then opens it in the studio", async () => {
    api.post.mockResolvedValue({ id: ID, title: "Weekly board" });
    draw();
    // One main button, as Videos and Presentations: its dialog also starts an empty dashboard.
    fireEvent.click(await screen.findByRole("button", { name: "New dashboard" }));
    const create = (await screen.findByTestId("dashboards.create.blank")) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "  Weekly board " } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "For the **SOC**" } });
    fireEvent.click(create);
    expect(await screen.findByText("studio of the new dashboard")).toBeTruthy();
    expect(api.post.mock.calls).toEqual([["/custom-dashboards", { title: "Weekly board", description: "For the **SOC**" }]]);
    expect(JSON.parse(screen.getByTestId("launch").textContent ?? "")).toEqual({});
  });

  it("creates a dashboard with the assistant: the studio opens with the brief to send and the agent that builds it", async () => {
    const builder = {
      id: "ag1", name: "Builder", enabled: true, disable_chat: false, is_company_managed: false,
      builtin_tool_ids: ["custom_dashboards"], builtin_allowed_tools: null,
    }; // prettier-ignore
    api.get.mockImplementation(async (path: string) =>
      path === "/auth/me" ? { id: "u1", home_dashboard_id: null } : path.startsWith("/agents") ? [builder] : [],
    );
    api.post.mockResolvedValue({ id: ID, title: "SOC brief" });
    draw();
    // The list's main button opens the same dialog, on the assistant's side.
    fireEvent.click(await screen.findByRole("button", { name: "New dashboard" }));
    const assist = (await screen.findByTestId("dashboards.create.assistant")) as HTMLButtonElement;
    expect(assist.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "SOC brief" } });
    fireEvent.change(screen.getByTestId("dashboards.create.brief"), { target: { value: "Critical detections by day" } });
    await waitFor(() => expect(assist.disabled).toBe(false));
    expect(screen.getByTestId("dashboards.create.request").textContent).toContain("Critical detections by day");
    fireEvent.click(assist);
    expect(await screen.findByText("studio of the new dashboard")).toBeTruthy();
    expect(api.post.mock.calls).toEqual([["/custom-dashboards", { title: "SOC brief" }]]);
    expect(JSON.parse(screen.getByTestId("launch").textContent ?? "")).toEqual({
      send: 'Build the dashboard "SOC brief":\n\nCritical detections by day',
      agentId: "ag1",
    });
  });

  it("imports a file as a new dashboard and opens it in the studio", async () => {
    api.postForm.mockResolvedValue({ id: ID, title: "Imported", import_notes: [] });
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const dialog = await screen.findByTestId("dashboards.import.dialog");
    const picked = new File(['{"format":"xtm-one-widget-pack"}'], "board.json", { type: "application/json" });
    fireEvent.change(within(dialog).getByTestId("dashboards.import.file"), { target: { files: [picked] } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Import" }));
    expect(await screen.findByText("studio of the new dashboard")).toBeTruthy();
    const [path, form] = api.postForm.mock.calls[0];
    expect([path, (form as FormData).get("file"), (form as FormData).get("bindings")]).toEqual(["/custom-dashboards/import", picked, null]);
  });

  it("offers which integration to read through where there are several, and sends the choice", async () => {
    api.get.mockImplementation(async (path: string) =>
      path === "/auth/me"
        ? { id: "u1", home_dashboard_id: null }
        : path === "/dashboard-widget-packs/integrations"
          ? [{ id: "i1", provider: "opencti", name: "Lab" }, { id: "i2", provider: "opencti", name: "Prod" }, { id: "x1", provider: "openaev", name: "Range" }]
          : [],
    );
    api.postForm.mockResolvedValue({ id: ID, title: "Imported", import_notes: [] });
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const dialog = await screen.findByTestId("dashboards.import.dialog");
    fireEvent.change(within(dialog).getByTestId("dashboards.import.file"), { target: { files: [new File(["{}"], "board.json")] } });
    const confirm = within(dialog).getByRole("button", { name: "Import" }) as HTMLButtonElement;
    // OpenCTI is a choice (two integrations); OpenAEV is implied (one). The choice is optional: the server says
    // which to pick when the file reads that platform.
    const picker = await within(dialog).findByRole("combobox", { name: "OpenCTI to read from" });
    expect(within(dialog).queryByRole("combobox", { name: "OpenAEV to read from" })).toBeNull();
    await waitFor(() => expect(confirm.disabled).toBe(false));
    await userEvent.setup().click(picker);
    await userEvent.setup().click(await screen.findByRole("option", { name: "Prod" }));
    fireEvent.click(confirm);
    await waitFor(() => expect(api.postForm).toHaveBeenCalledTimes(1));
    expect(JSON.parse((api.postForm.mock.calls[0][1] as FormData).get("bindings") as string)).toEqual({ opencti: "i2" });
  });

  it("lists dashboards as a table, sorted by a column, filtered by tag", async () => {
    window.localStorage.setItem("copilot-view:dashboards", "list");
    api.get.mockImplementation(async (path: string) => (path === "/auth/me" ? { id: "u1", home_dashboard_id: null } : BOARDS));
    // Every segment: the page lands on Mine, which holds no shared board.
    draw("/dashboards?vis=all");
    const names = () => screen.getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[1].textContent);
    await waitFor(() => expect(names()).toEqual(["Weekly review", "Their board", "Exposure"]));
    fireEvent.click(screen.getByRole("button", { name: /Name/ }));
    expect(names()).toEqual(["Exposure", "Their board", "Weekly review"]);
    // Standard v2: the tags are one "Tags" pill on the table's toolbar, opened first.
    // The pill is the "Tags" button that opens a panel (the column header is a sort button).
    const tagsPill = screen.getAllByRole("button", { name: /^Tags/ }).find((button) => button.hasAttribute("aria-expanded"));
    fireEvent.click(tagsPill!);
    fireEvent.click(within(screen.getByRole("group", { name: "Tags" })).getByRole("button", { name: "ctem" }));
    expect(names()).toEqual(["Exposure"]);
  });

  it("duplicates a dashboard, and files one under tags", async () => {
    api.get.mockImplementation(async (path: string) => (path === "/auth/me" ? { id: "u1", home_dashboard_id: null } : path === "/tags" ? [] : BOARDS));
    api.post.mockResolvedValue({ ...BOARDS[2], id: "d", title: "Their board (copy)" });
    api.put.mockResolvedValue(BOARDS[0]);
    draw("/dashboards?vis=all");
    fireEvent.pointerDown(await screen.findByRole("button", { name: "Actions on Their board" }), { button: 0, ctrlKey: false });
    // A reader duplicates it, and may not tag or delete it.
    expect((await screen.findAllByRole("menuitem")).map((item) => item.textContent)).toEqual(["Duplicate"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Duplicate" }));
    await waitFor(() => expect(api.post.mock.calls).toEqual([["/custom-dashboards/c/duplicate"]]));
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions on Weekly review" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Edit tags" }));
    fireEvent.click(within(screen.getByTestId("dashboards.tags.dialog")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.put.mock.calls).toEqual([["/custom-dashboards/a/tags", { tags: ["soc"] }]]));
  });

  it("deletes the owner's selected dashboards together, after asking", async () => {
    window.localStorage.setItem("copilot-view:dashboards", "list");
    api.get.mockImplementation(async (path: string) => (path === "/auth/me" ? { id: "u1", home_dashboard_id: null } : BOARDS));
    api.delete.mockResolvedValue(undefined);
    draw();
    // Only the owner's rows can be picked.
    await screen.findByRole("checkbox", { name: "Select Weekly review" });
    expect(screen.queryByRole("checkbox", { name: "Select Their board" })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Weekly review" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Exposure" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.delete.mock.calls.map(([path]) => path).sort()).toEqual(["/custom-dashboards/a", "/custom-dashboards/b"]));
  });

  it("lets an administrator review every public link and turn one off", async () => {
    api.get.mockImplementation(async (path: string) =>
      path === "/auth/me"
        ? { id: "u1", role: "admin", home_dashboard_id: null }
        : path === "/custom-dashboards/public-links"
          ? [{ dashboard_id: "a", dashboard_title: "Weekly review", publisher_name: "Ada", created_at: null, refresh_minutes: 60, snapshot_at: null }]
          : BOARDS,
    );
    api.delete.mockResolvedValue(undefined);
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Public links" }));
    const dialog = await screen.findByTestId("dashboards.public-links");
    expect((await within(dialog).findByTestId("dashboards.public-links.row")).textContent).toContain("Published by Ada");
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(api.delete.mock.calls).toEqual([["/custom-dashboards/public-links/a"]]));
  });

  it("offers no review of public links to someone who is not an administrator", async () => {
    draw();
    await screen.findByRole("button", { name: "New dashboard" });
    expect(screen.queryByRole("button", { name: "Public links" })).toBeNull();
  });

  it("offers neither without the Dashboard Studio", async () => {
    flags.studio = false;
    draw();
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: "Widget packs" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
  });
});
