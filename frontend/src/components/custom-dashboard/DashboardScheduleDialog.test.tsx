// Scheduling a dashboard's export: an assignment on an agent with the
// Dashboards tools (others are offered disabled), created through the
// assignment API with what it sends; this dashboard's schedules are listed.
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardScheduleDialog } from "./DashboardScheduleDialog";

const DASHBOARD = { id: "d-1", title: "Ops", revision: 3, can_manage: true, spec: {} } as unknown as CustomDashboard;

const AGENTS = [
  { id: "a-1", name: "Dashboard Designer", builtin_tool_ids: ["custom_dashboards"], can_manage: true },
  { id: "a-2", name: "Analyst", builtin_tool_ids: [], can_manage: true },
];

const SCHEDULED = {
  id: "s-1",
  agent_id: "a-1",
  name: "Send the dashboard Ops",
  enabled: true,
  trigger_type: "schedule",
  trigger_config: { cron: "0 8 * * 1-5", dashboard_export: { dashboard_id: "d-1", format: "pdf", recipients: ["ann@acme.example"] } },
};
const OTHER = { ...SCHEDULED, id: "s-2", trigger_config: { cron: "0 8 * * *", dashboard_export: { dashboard_id: "d-2", format: "png", recipients: [] } } };

describe("DashboardScheduleDialog", () => {
  it("lists this dashboard's schedules and schedules a new one", async () => {
    api.get.mockImplementation(async (url: string) => (url.startsWith("/agents") ? AGENTS : [SCHEDULED, OTHER]));
    api.post.mockResolvedValue({ id: "s-3" });
    const user = userEvent.setup();
    renderWithProviders(<DashboardScheduleDialog dashboard={DASHBOARD} open onOpenChange={vi.fn()} />);
    const rows = await screen.findAllByTestId("dashboard.schedule.row");
    expect(rows.map((row) => within(row).getByText(/sends it to/).textContent)).toEqual(["Dashboard Designer sends it to ann@acme.example"]);

    await user.click(screen.getByRole("combobox", { name: "Agent" }));
    expect((screen.getByRole("option", { name: /Analyst/ }) as HTMLElement).getAttribute("aria-disabled")).toBe("true");
    await user.click(screen.getByRole("option", { name: "Dashboard Designer" }));
    await user.type(screen.getByRole("textbox", { name: "Recipients" }), "Bob@acme.example, carl@acme.example");
    await user.click(screen.getByRole("button", { name: "Schedule" }));
    const [path, body] = api.post.mock.calls.find(([url]) => url === "/assignments") ?? [];
    expect(path).toBe("/assignments");
    expect(body.agent_id).toBe("a-1");
    expect(body.trigger_type).toBe("schedule");
    expect(body.trigger_config).toEqual({
      cron: "0 8 * * 1-5",
      dashboard_export: { dashboard_id: "d-1", format: "pdf", recipients: ["bob@acme.example", "carl@acme.example"] },
    });
    expect(body.prompt.split("\n")[0]).toBe('Send the dashboard "Ops" (dashboard_id d-1) as a PDF with the send_dashboard_export tool.');
  }, 20_000);

  it("refuses to schedule without valid recipients", async () => {
    api.get.mockImplementation(async (url: string) => (url.startsWith("/agents") ? AGENTS : []));
    const user = userEvent.setup();
    renderWithProviders(<DashboardScheduleDialog dashboard={DASHBOARD} open onOpenChange={vi.fn()} />);
    await user.click(await screen.findByRole("combobox", { name: "Agent" }));
    await user.click(screen.getByRole("option", { name: "Dashboard Designer" }));
    await user.type(screen.getByRole("textbox", { name: "Recipients" }), "not-an-address");
    expect(screen.getByText("Not an email address: not-an-address")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Schedule" }) as HTMLButtonElement).disabled).toBe(true);
  }, 20_000);
});
