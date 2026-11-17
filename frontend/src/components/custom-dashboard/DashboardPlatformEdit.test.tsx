// Editing a platform widget never drops what it holds: the editor shows a
// relationships widget's source filters and writes back what it does not
// change, a widget of a type it does not know opens in the source editor (the
// form refuses to stand in for it), and saving an unchanged widget writes nothing.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { PLATFORM_FORMS } from "@/testing/platform-forms";

import { DashboardDataSourcesPanel } from "./DashboardDataSourcesPanel";
import { PlatformSourceEditor } from "./PlatformSourceEditor";

const ID = "11111111-1111-4111-8111-111111111111";
const INTEGRATION = "33333333-3333-4333-8333-333333333333";

// As the server stores it: its defaults written out.
const DONUT = {
  type: "donut",
  perspective: "entities",
  dataSelection: [
    {
      label: "",
      number: 8,
      attribute: "created-by.internal_id",
      date_attribute: "published",
      isTo: true,
      perspective: "entities",
      filters: { mode: "and", filters: [{ key: ["entity_type"], values: ["Report"], operator: "eq", mode: "or" }], filterGroups: [] },
      dynamicFrom: { mode: "and", filters: [], filterGroups: [] },
      dynamicTo: { mode: "and", filters: [], filterGroups: [] },
    },
  ],
  parameters: { title: "Reports by author" },
};
// Relationships whose source is a malware.
const MALWARE = { mode: "and", filters: [{ key: ["entity_type"], values: ["Malware"], operator: "eq", mode: "or" }], filterGroups: [] };
const TARGETED = {
  ...DONUT,
  perspective: "relationships",
  dataSelection: [
    {
      ...DONUT.dataSelection[0],
      perspective: "relationships",
      filters: { mode: "and", filters: [{ key: ["relationship_type"], values: ["uses"], operator: "eq", mode: "or" }], filterGroups: [] },
      dynamicFrom: MALWARE,
    },
  ],
};

const dashboard = {
  id: ID,
  revision: 3,
  can_manage: true,
  widgets: [],
  spec: {
    version: 2,
    title: "Board",
    dataSources: {
      authors: { type: "platform", name: "Authors", options: { source: "opencti", integration_id: INTEGRATION, native: DONUT } },
      targeted: { type: "platform", name: "Targeted", options: { source: "opencti", integration_id: INTEGRATION, native: TARGETED } },
      // A type this editor does not know (a newer OpenCTI's).
      newer: { type: "platform", name: "Newer", options: { source: "opencti", integration_id: INTEGRATION, native: { ...DONUT, type: "sunburst" } } },
    },
    visualizations: {},
    layout: { tabs: { items: [{ layoutId: "main", label: "Main" }] }, layoutDefinitions: { main: { structure: [] } } },
  },
} as unknown as CustomDashboard;

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  api.get.mockImplementation(async (url: string) =>
    url === "/dashboard-widget-packs/integrations"
      ? [{ id: INTEGRATION, provider: "opencti", name: "Lab" }]
      : url === "/custom-dashboards/platform-forms"
        ? PLATFORM_FORMS
        : [],
  );
  api.post.mockResolvedValue({ status: "ok", items: [], code: null });
});

function open(edit = vi.fn(), onEditSource = vi.fn()) {
  renderWithProviders(
    <DashboardDataSourcesPanel dashboard={dashboard} layoutId="main" edit={edit} onEditSource={onEditSource} onVisualizationAdded={vi.fn()} />,
  );
  return { edit, onEditSource };
}

async function editWidget(name: string) {
  fireEvent.pointerDown(screen.getByRole("button", { name: `Actions on ${name}` }), { button: 0, ctrlKey: false });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Edit the widget" }));
}

describe("editing a platform widget", () => {
  it("opens a widget the form cannot carry in the source editor, never as a form", async () => {
    const { edit, onEditSource } = open();
    await editWidget("Newer");
    await waitFor(() => expect(onEditSource.mock.calls).toEqual([["newer"]]));
    expect(screen.queryByTestId("dashboard.sources.edit-platform")).toBeNull();
    expect(edit.mock.calls).toEqual([]);
  });

  it("opens a widget the form carries as its form, and writes nothing when saved unchanged", async () => {
    const { edit, onEditSource } = open();
    await editWidget("Authors");
    expect(await screen.findByTestId("dashboard.sources.edit-platform")).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByTestId("dashboard.sources.edit-platform")).toBeNull());
    expect(edit.mock.calls).toEqual([]);
    expect(onEditSource.mock.calls).toEqual([]);
  });

  it("shows a relationships widget's source filters and saves a change without losing them", async () => {
    const { edit } = open();
    await editWidget("Targeted");
    const dialog = await screen.findByTestId("dashboard.sources.edit-platform");
    expect(await within(dialog).findByText("Source filters")).toBeTruthy();
    expect(within(dialog).getAllByTestId("dashboard.sources.filter")).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole("switch", { name: "Display the source" }));
    const save = within(dialog).getByRole("button", { name: "Save" });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(edit.mock.calls).toHaveLength(1));
    const [[operations]] = edit.mock.calls;
    expect(operations).toEqual([
      {
        op: "set",
        path: "dataSources.targeted",
        value: {
          type: "platform",
          name: "Targeted",
          options: {
            source: "opencti",
            integration_id: INTEGRATION,
            native: { ...TARGETED, dataSelection: [{ ...TARGETED.dataSelection[0], isTo: false }] },
          },
        },
      },
    ]);
  });

  it("the form editor gives nothing to save for a widget it cannot carry", async () => {
    const onChange = vi.fn();
    renderWithProviders(
      <PlatformSourceEditor dashboardId={ID} initial={{ source: "opencti", integration_id: INTEGRATION, native: { ...DONUT, type: "sunburst" } }} onChange={onChange} />,
    );
    expect(await screen.findByTestId("dashboard.sources.platform.not-carried")).toBeTruthy();
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(onChange.mock.calls.every(([options]) => options === null)).toBe(true);
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
  });
});
