// The platform source editor: a stored OpenCTI or OpenAEV widget opens in the
// one form, drawn from its platform's description, and comes back unchanged,
// every list is a question to the platform through the person's integration,
// each control changes only what it is about, and Preview reads the widget
// before it is saved.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { PLATFORM_FORMS, platformForm } from "@/testing/platform-forms";

import { edit, newWidget } from "./platform-form";
import { PlatformSourceEditor } from "./PlatformSourceEditor";

const cti = platformForm("opencti");
const aev = platformForm("openaev");
const DONUT = newWidget(cti, "donut", { perspective: "entities" });
const STORED = [
  (w: Record<string, unknown>) => edit(cti, w, "types", ["Report"], [0]),
  (w: Record<string, unknown>) => edit(cti, w, "attribute", "created-by.internal_id", [0]),
  (w: Record<string, unknown>) => edit(cti, w, "number", 8, [0]),
].reduce((widget, change) => change(widget), DONUT);

function lastOptions(onChange: ReturnType<typeof vi.fn>) {
  return onChange.mock.calls[onChange.mock.calls.length - 1]?.[0];
}

describe("PlatformSourceEditor", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
    api.get.mockImplementation(async (url: string) =>
      url === "/custom-dashboards/platform-forms"
        ? PLATFORM_FORMS
        : [
            { id: "i1", provider: "opencti", name: "Lab" },
            { id: "x1", provider: "openaev", name: "Range" },
            { id: "q1", provider: "opencrq", name: "Risk" },
          ],
    );
    api.post.mockImplementation(async (url: string, body: { kind?: string; source?: string }) => {
      if (url.endsWith("/widgets/preview")) {
        return { status: "ok", shape: "distribution", data: { items: [{ label: "CERT-EU", value: 4 }] }, fetched_at: "2026-10-25T10:00:00Z", open_url: null };
      }
      if (body.source === "openaev") {
        return {
          status: "ok",
          code: null,
          items: [
            { key: "base_created_at", label: "Created", type: "instant", entity: "finding" },
            { key: "finding_type", label: "Type", type: "string", entity: "finding", values: ["cve", "port"] },
          ],
        };
      }
      const items: Record<string, unknown[]> = {
        entity_types: [{ value: "Report", label: "Report", group: "sdos" }, { value: "Malware", label: "Malware", group: "sdos" }],
        filter_keys: [{ key: "objectLabel", label: "Label", type: "id", multiple: true, search: ["Label"] }],
        attributes: [{ value: "entity_type", label: "entity_type" }, { value: "created-by.internal_id", label: "created-by.internal_id" }],
        saved_filters: [{ value: "s1", label: "Weekly reports" }],
      };
      return { status: "ok", items: items[body.kind ?? ""] ?? [], code: null };
    });
  });

  it("opens a stored OpenCTI widget in the form and gives it back unchanged", async () => {
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={onChange} />);
    await waitFor(() => expect(lastOptions(onChange)).toEqual({ source: "opencti", integration_id: "i1", native: STORED }));
    await waitFor(() => expect(api.post.mock.calls.some(([, body]) => body.kind === "attributes")).toBe(true));
    const attributes = api.post.mock.calls.find(([, body]) => body.kind === "attributes")?.[1];
    expect(attributes).toEqual({ source: "opencti", integration_id: "i1", perspective: "entities", kind: "attributes", types: ["Report"] });
    // The types filter is the types picker's, not a filter row.
    expect(screen.queryAllByTestId("dashboard.sources.filter")).toHaveLength(0);
  });

  it("reads a selection's filters from one of the person's saved filters, its own filters set aside", async () => {
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={onChange} />);
    fireEvent.click(await screen.findByRole("combobox", { name: "Saved filter" }));
    fireEvent.click(await screen.findByRole("option", { name: "Weekly reports" }));
    await waitFor(() => expect(lastOptions(onChange).native.dataSelection[0].filters_id).toBe("s1"));
    expect(lastOptions(onChange).native.dataSelection[0].filters).toEqual({ mode: "and", filters: [], filterGroups: [] });
    // The filters editor gives way to the saved filter.
    expect(screen.queryByRole("button", { name: "Add a filter" })).toBeNull();
    await waitFor(() => expect(api.post.mock.calls.some(([, body]) => body.kind === "saved_filters" && body.perspective === "entities")).toBe(true));
  });

  it("changes only what a control is about", async () => {
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={onChange} />);
    const top = await screen.findByRole("spinbutton", { name: "Number of results" });
    fireEvent.change(top, { target: { value: "25" } });
    await waitFor(() => expect(lastOptions(onChange).native.dataSelection[0].number).toBe(25));
    expect(lastOptions(onChange).native).toEqual(edit(cti, STORED, "number", 25, [0]));
  });

  it("previews the widget with the person's access before it is saved", async () => {
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(api.post.mock.calls.filter(([url]) => url.endsWith("/widgets/preview"))).toEqual([
        ["/custom-dashboards/d1/widgets/preview", { widget: { kind: "platform", id: "preview", source: "opencti", integration_id: "i1", native: STORED } }],
      ]),
    );
  });

  it("draws the preview in its module's view and accent, else by its data's shape", async () => {
    const answer = { status: "ok", shape: "distribution", data: { items: [{ label: "CERT-EU", value: 4 }] }, fetched_at: "2026-10-25T10:00:00Z", open_url: null };
    const preview = async (presentation?: unknown) => {
      const post = api.post.getMockImplementation()!;
      api.post.mockImplementation(async (url: string, body: never) => (url.endsWith("/widgets/preview") ? { ...answer, presentation } : post(url, body)));
      const view = renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
      const drawn = await screen.findByTestId("dashboard.sources.platform.preview");
      const seen = [drawn.getAttribute("data-view"), drawn.style.getPropertyValue("--color-accent")];
      view.unmount();
      api.post.mockImplementation(post);
      return seen;
    };
    expect(await preview({ view: "xtm.bar", options: { accentColor: "brand" } })).toEqual(["hbar", "var(--color-module-brand)"]);
    expect(await preview()).toEqual(["donut", ""]);
  });

  it("adds a filter row from the platform's filter keys, and a nested filter group", async () => {
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencti", integration_id: "i1", native: STORED }} onChange={onChange} />);
    const add = await screen.findByRole("button", { name: "Add a filter" });
    await waitFor(() => expect((add as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(add);
    expect(await screen.findByRole("button", { name: "Remove the filter" })).toBeTruthy();
    await waitFor(() => expect(api.post.mock.calls.some(([, body]) => body.kind === "values" && body.key === "objectLabel")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Add a filter group" }));
    await waitFor(() =>
      expect(lastOptions(onChange).native.dataSelection[0].filters).toEqual({
        mode: "and",
        filters: [
          { key: ["entity_type"], values: ["Report"], operator: "eq", mode: "or" },
          { key: ["objectLabel"], values: [], operator: "eq", mode: "or" },
        ],
        filterGroups: [{ mode: "and", filters: [], filterGroups: [] }],
      }),
    );
  });

  it("edits an OpenAEV list's columns and sorts, and keeps its filters' ids", async () => {
    const list = newWidget(aev, "list", { entity: "finding" });
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "openaev", integration_id: "x1", native: list }} onChange={onChange} />);
    await waitFor(() => expect(lastOptions(onChange)?.native).toEqual(list));
    expect(await screen.findByTestId("dashboard.sources.columns")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add a sort" }));
    await waitFor(() => expect(lastOptions(onChange).native.widget_config.sorts).toEqual([{ fieldName: "base_created_at", direction: "DESC" }]));
    expect(lastOptions(onChange).native.widget_config.perspective).toEqual((list.widget_config as { perspective: unknown }).perspective);
  });

  it("builds OpenAEV's security coverage series from detection or prevention", async () => {
    const coverage = newWidget(aev, "security-coverage");
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "openaev", integration_id: "x1", native: coverage }} onChange={onChange} />);
    const expectation = await screen.findByRole("group", { name: "Expectation" });
    fireEvent.click(within(expectation).getByRole("button", { name: "Prevention" }));
    await waitFor(() =>
      expect(
        lastOptions(onChange).native.widget_config.series.map((s: { filter: { filters: { key: string; values: string[] }[] } }) =>
          s.filter.filters.find((f) => f.key === "inject_expectation_type")?.values,
        ),
      ).toEqual([["PREVENTION"], ["PREVENTION"]]),
    );
  });
  it("adds an OpenAEV series of the same entity, up to its type's limit", async () => {
    const bars = newWidget(aev, "vertical-barchart", { entity: "finding" });
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "openaev", integration_id: "x1", native: bars }} onChange={onChange} />);
    expect(await screen.findByText("1 of 5 series")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add a series" }));
    await waitFor(() => expect(lastOptions(onChange).native.widget_config.series).toHaveLength(2));
    const [first, second] = lastOptions(onChange).native.widget_config.series;
    expect(first).toEqual((bars.widget_config as { series: unknown[] }).series[0]);
    expect(second.filter.filters.map((f: { key: string; values: string[] }) => [f.key, f.values])).toEqual([["base_entity", ["finding"]]]);
    expect(screen.getByRole("button", { name: "Remove series 2" })).toBeTruthy();
  });

  it("edits an OpenCRQ metric with the same form, asking OpenCRQ nothing", async () => {
    const trend = { metric: "exposure_trend_ale", organization_id: "o1", window_days: 30 };
    const onChange = vi.fn();
    renderWithProviders(<PlatformSourceEditor dashboardId="d1" initial={{ source: "opencrq", integration_id: "q1", native: trend }} onChange={onChange} />);
    const days = await screen.findByRole("spinbutton", { name: "Days" });
    expect((days as HTMLInputElement).value).toBe("30");
    fireEvent.change(days, { target: { value: "45" } });
    await waitFor(() => expect(lastOptions(onChange)).toEqual({ source: "opencrq", integration_id: "q1", native: { ...trend, window_days: 45 } }));
    expect(api.post.mock.calls.filter(([url]) => url.endsWith("/platform-schema"))).toEqual([]);
  });
});
