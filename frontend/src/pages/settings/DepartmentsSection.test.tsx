// Settings > Organization > Departments: the mapping, its preview, the rule order saved and the HR import;
// its self-explanation: the help panel, the reason a button is disabled, the departments created from the org-unit groups.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { DepartmentMappingResponse, DepartmentPreview, Group } from "@/types";
import { DepartmentsSection } from "./DepartmentsSection";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);
const post = vi.mocked(api.post);

function group(id: string, name: string, kind: Group["kind"]): Group {
  return {
    id,
    name,
    description: null,
    member_count: 3,
    created_by: null,
    quota_config: null,
    kind,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
  };
}

const MAPPING: DepartmentMappingResponse = {
  departments: [
    { id: "d-eng", name: "Engineering", description: null, directory_values: ["R&D"] },
    { id: "d-fin", name: "Finance", description: null, directory_values: [] },
  ],
  rules: [
    { group_id: "g-fin", department: "Finance", group_name: "Finance Team", group_kind: "org_unit" },
    { group_id: "g-plat", department: "Engineering", group_name: "Platform", group_kind: "org_unit" },
  ],
  settings: { sources: ["directory", "groups"], directory_attribute: "department" },
  backfill: { started: true, completed_at: null, cursor_date: "2026-06-01", rewrite_requested_at: null },
};

const EMPTY: DepartmentMappingResponse = {
  departments: [],
  rules: [],
  settings: { sources: ["directory", "groups"], directory_attribute: "department" },
  backfill: { started: false, completed_at: null, cursor_date: null, rewrite_requested_at: null },
};

const PREVIEW: DepartmentPreview = {
  configured: true,
  user_count: 5,
  resolved_count: 4,
  unassigned_count: 1,
  departments: [
    { department_id: "d-eng", name: "Engineering", user_count: 3, users: [] },
    { department_id: "d-fin", name: "Finance", user_count: 1, users: [] },
  ],
  unassigned: [{ user_id: "u-5", email: "eve@example.test", full_name: "Eve", directory_department: null }],
};

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/insights/departments") return MAPPING;
    if (path === "/insights/departments/preview") return PREVIEW;
    if (path === "/groups") return [group("g-fin", "Finance Team", "org_unit"), group("g-plat", "Platform", "org_unit")];
    throw new Error(`unexpected GET ${path}`);
  });
  put.mockImplementation(async (_path: string, body: unknown) => ({ ...MAPPING, ...(body as object) }));
  post.mockResolvedValue({ entries: 2, updated: 1, unknown_count: 1, unknown_emails: ["ghost@example.test"] });
});

describe("DepartmentsSection", () => {
  it("shows the departments, the rules in their order and who the mapping resolves where", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    const [first] = await screen.findAllByTestId("settings.departments.department-row");
    expect(first).toHaveTextContent("Engineering");
    expect(screen.getByText("Finance Team")).toBeTruthy();
    // At rest (#10855): the settings and the department rows read as text, never as disabled fields.
    expect(screen.getByTestId("settings.departments.read")).toBeTruthy();
    expect(document.querySelectorAll("input:disabled, textarea:disabled, button[role=combobox]:disabled")).toHaveLength(0);
    expect(await screen.findByText("4 of 5 active people resolved, 1 unassigned")).toBeTruthy();
    expect(screen.getByText(/done back to 2026-06-01/)).toBeTruthy();
  });

  it("saves the group rules in the order the administrator set", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    await screen.findAllByTestId("settings.departments.department-row");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Move down" }))[0]);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [path, body] = put.mock.calls[0] as [string, { rules: { group_id: string; department: string }[] }];
    expect(path).toBe("/insights/departments");
    expect(body.rules).toEqual([
      { group_id: "g-plat", department: "Engineering" },
      { group_id: "g-fin", department: "Finance" },
    ]);
  });

  it("takes the directory values one per entry, as typed, never as a comma-separated text", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    await screen.findAllByTestId("settings.departments.department-row");
    expect(screen.getByText("R&D")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const [engineering] = await screen.findAllByRole("textbox", { name: "Directory values (optional)" });
    fireEvent.change(engineering, { target: { value: "Research" } });
    fireEvent.keyDown(engineering, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, DepartmentMappingResponse];
    expect(body.departments[0].directory_values).toEqual(["R&D", "Research"]);
  });

  it("imports the directory departments of an HR export, header line skipped", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    const field = await screen.findByLabelText("Email and department per line");
    fireEvent.change(field, { target: { value: "email,department\nada@example.test,Engineering\nghost@example.test;Sales" } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/insights/departments/directory", {
        entries: [
          { email: "ada@example.test", department: "Engineering" },
          { email: "ghost@example.test", department: "Sales" },
        ],
      }),
    );
  });

  it("explains the three steps and what a directory value is", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    await screen.findAllByTestId("settings.departments.department-row");
    expect(screen.getByText("How does it work?")).toBeTruthy();
    expect(screen.getByText(/Create one department per org unit/)).toBeTruthy();
    expect(screen.getByText(/the group "Marketing" counts for the department "Marketing"/)).toBeTruthy();
    expect(screen.getByText("Directory values (optional)")).toBeTruthy();
    expect(screen.getByText(/One department per org unit, its name unique/)).toBeTruthy();
    // Every column of the rows carries its own "About this field" bubble, and every row's field is named by the visible column label.
    expect(screen.getAllByRole("img", { name: "About this field" }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("img", { name: "Directory values (optional)" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findAllByRole("textbox", { name: "Name" })).toHaveLength(2);
  });

  it("lists the departments in the shared data table: search, sort, people count, every row on one page while edited", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    await screen.findAllByTestId("settings.departments.department-row");
    // Read mode: text, not disabled inputs; a department without directory values reads "Not set".
    expect(screen.queryAllByRole("textbox", { name: "Name" })).toHaveLength(0);
    expect(screen.getByText("Not set")).toBeTruthy();
    expect(await screen.findByRole("button", { name: /^People/ })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "R&D" } });
    await waitFor(() => expect(screen.getAllByTestId("settings.departments.department-row")).toHaveLength(1));
    expect(screen.getByTestId("settings.departments.department-row")).toHaveTextContent("Engineering");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });

    fireEvent.click(screen.getByRole("button", { name: /^Name/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Name/ }));
    await waitFor(() => expect(screen.getAllByTestId("settings.departments.department-row")[0]).toHaveTextContent("Finance"));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findAllByRole("textbox", { name: "Name" })).toHaveLength(2);
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Remove the department" })).toHaveLength(2);
  });

  it("puts Cancel before Save, marks the name required and says inline why an empty or duplicate name cannot be saved", async () => {
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    await screen.findAllByTestId("settings.departments.department-row");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    const save = screen.getByRole("button", { name: "Save" });
    expect(cancel.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The Name header of the departments table itself carries the marker (UX r1 B2 on #10853),
    // not only the group rule columns below it.
    const table = within(screen.getAllByTestId("settings.departments.department-row")[0].closest("table")!);
    const nameHeader = table.getAllByRole("columnheader").find((th) => th.textContent?.startsWith("Name"))!;
    expect(within(nameHeader).getByLabelText("Required")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Add a department" }));
    const names = screen.getAllByRole("textbox", { name: /^Name/ });
    const added = names[names.length - 1];
    expect(added).toHaveAttribute("aria-invalid", "true");
    expect(added).toHaveAccessibleDescription("Enter a name.");

    fireEvent.change(added, { target: { value: "engineering" } });
    expect(added).toHaveAccessibleDescription("Two departments have the same name.");
    expect(names[0]).toHaveAttribute("aria-invalid", "true");
    expect(save).toBeDisabled();

    fireEvent.change(added, { target: { value: "Marketing" } });
    expect(added).not.toHaveAttribute("aria-invalid");
  });

  it("says why a group rule cannot be added yet, and creates the departments from the org-unit groups", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/insights/departments") return EMPTY;
      if (path === "/groups") return [group("g-mkt", "Marketing", "org_unit"), group("g-vpn", "VPN", "access")];
      throw new Error(`unexpected GET ${path}`);
    });
    renderWithProviders(<MemoryRouter><DepartmentsSection /></MemoryRouter>);
    expect(await screen.findByText(/No department yet: everyone reads as Unassigned/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const addRule = await screen.findByRole("button", { name: "Add a group rule" });
    expect(addRule).toBeDisabled();
    expect(addRule.parentElement).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(screen.getByRole("button", { name: "Create from the org-unit groups" }));
    expect((await screen.findAllByDisplayValue("Marketing")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Add a group rule" })).not.toBeDisabled();
    expect(screen.queryByDisplayValue("VPN")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, DepartmentMappingResponse];
    expect(body.departments).toEqual([{ name: "Marketing", directory_values: [] }]);
    expect(body.rules).toEqual([{ group_id: "g-mkt", department: "Marketing" }]);
  });
});
