// A dashboard's history compares the picked version with another (the
// current one by default), older first, and says which widgets changed.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardHistoryDialog } from "./DashboardHistoryDialog";

const DASHBOARD = { id: "d-1", title: "Board brief", revision: 3, can_manage: true, spec: { title: "Board brief", widgets: [] } } as unknown as CustomDashboard;

const version = (revision: number, summary: string) => ({
  revision,
  title: "Board brief",
  widget_count: 1,
  source: "manual",
  summary,
  restored_from_revision: null,
  author_id: null,
  author_name: "Editor",
  created_at: "2026-10-25T10:00:00Z",
});

function answer(url: string) {
  if (url.endsWith("/versions")) return Promise.resolve([version(3, "renamed"), version(2, "removed note"), version(1, "")]);
  if (url.includes("/versions/diff")) {
    return Promise.resolve({
      from_version: 1,
      to_version: 3,
      added: 1,
      removed: 1,
      changes: "removed note; renamed",
      lines: [
        { type: "remove", content: '"title": "Board brief",', old_line: 2, new_line: null },
        { type: "add", content: '"title": "Board brief v2",', old_line: null, new_line: 2 },
      ],
      unified_diff: "",
    });
  }
  return Promise.resolve({ ...version(Number(url.split("/").pop()), ""), spec: { title: "Board brief", widgets: [] } });
}

describe("DashboardHistoryDialog", () => {
  it("compares the picked version with the current one, older first", async () => {
    api.get.mockImplementation(answer);
    const user = userEvent.setup();
    renderWithProviders(<DashboardHistoryDialog dashboard={DASHBOARD} open onOpenChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Revision 1/ }));
    await user.click(screen.getByRole("button", { name: "Compare" }));
    const dialog = await screen.findByRole("dialog", { name: "Revision 1 and revision 3" });
    expect(within(dialog).getByText("removed note; renamed")).toBeTruthy();
    expect(api.get.mock.calls.filter(([url]) => String(url).includes("/versions/diff")).map(([url]) => url)).toEqual([
      "/custom-dashboards/d-1/versions/diff?from=1&to=3",
    ]);
  });

  it("compares with another version picked", async () => {
    api.get.mockReset();
    api.get.mockImplementation(answer);
    const user = userEvent.setup();
    renderWithProviders(<DashboardHistoryDialog dashboard={DASHBOARD} open onOpenChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Revision 3/ }));
    await user.click(screen.getByRole("combobox", { name: "Compare with" }));
    await user.click(screen.getByRole("option", { name: "Revision 1" }));
    await user.click(screen.getByRole("button", { name: "Compare" }));
    await screen.findByRole("dialog", { name: "Revision 1 and revision 3" });
    expect(api.get.mock.calls.filter(([url]) => String(url).includes("/versions/diff")).map(([url]) => url)).toEqual([
      "/custom-dashboards/d-1/versions/diff?from=1&to=3",
    ]);
  });
});
