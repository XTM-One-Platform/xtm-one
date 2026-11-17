// The values, lists, cards, timelines, texts and maps of the engine, drawn
// from answers as each module's adapter sends them (`dashboard_live/opencti.py`,
// `openaev.py`) with their presentation presets: what the viewer reads (labels,
// counts, colours, links), the empty, sample and error states, and the clicks
// handed to the dashboard's interactions.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetChartData, WidgetData, WidgetStudioData } from "@/types";

// happy-dom lays nothing out: every measured box is 600 x 300.
vi.mock("@/components/presentation/measured-box", () => ({ useMeasuredBox: () => ({ width: 600, height: 300 }) }));
const api = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));

import { CustomDashboardView } from "../CustomDashboardView";
import type { ClickEvent } from "../interactions";
import { normalizeDashboard, type PlatformView } from "../spec";
import { addChart } from "../viz-config";
import PlatformChart from "./PlatformChart";
import { formatNamedDate } from "./formats";
import StudioViz from "./StudioViz";

/** A moment as the viewer's locale writes it in a named format (the tests' locale is English). */
const written = (value: string, format: Parameters<typeof formatNamedDate>[1]) => formatNamedDate(value, format, "en");

type Ok = Extract<WidgetData, { status: "ok" }>;
type Chart = Ok & WidgetChartData;
const ok = <T extends Ok>(data: Omit<T, "status" | "fetched_at" | "open_url">): T =>
  ({ status: "ok", fetched_at: "2026-10-25T10:00:00Z", open_url: null, ...data }) as T;

function chart(data: Chart, view: PlatformView, options: Record<string, unknown>, onSelect?: (event: ClickEvent) => void) {
  return renderWithProviders(<PlatformChart data={data} view={view} height={300} options={options} onSelect={onSelect} />);
}
function studio(data: Ok, view: PlatformView, options: Record<string, unknown>) {
  return renderWithProviders(<StudioViz data={data} view={view} height={300} options={options} />);
}

// ── Fixtures: what each adapter sends ────────────────────────────────────────

const CTI_NUMBER = ok<Chart>({ shape: "number", data: { value: 1500, change: 3, previous: 1497, period: "day" } });
const AEV_NUMBER = ok<Chart>({
  shape: "number",
  data: { value: 1500, change: 100, previous: 1400, period: "interval", icon: { name: "zap", color: "warning" } },
});

const CTI_LIST = ok<Chart>({
  shape: "rows",
  data: {
    columns: [
      { key: "entity_type", label: "Type", kind: "chip" },
      { key: "name", label: "Name" },
      { key: "created", label: "Original creation date", kind: "date" },
      { key: "objectLabel", label: "Labels", kind: "chips" },
      { key: "objectMarking", label: "Marking", kind: "chips" },
    ],
    rows: [
      {
        entity_type: { label: "Malware", color: "#F0B60A", icon: "biohazard" },
        name: "Emotet",
        created: "2026-09-30T08:00:00Z",
        objectLabel: [{ label: "apt", color: "#ff0000" }, { label: "ransomware" }],
        objectMarking: [{ label: "TLP:AMBER", color: "alert" }],
        _url: "https://cti.example/dashboard/id/m1",
        _entityType: "Malware",
      },
      { entity_type: { label: "Report", color: "#70B23B", icon: "file-text" }, name: "Q3", created: null, objectLabel: [], objectMarking: [] },
    ],
    total: 12,
  },
});
const AEV_LIST = ok<Chart>({
  shape: "rows",
  data: {
    columns: [
      { key: "base_representative", label: "Representative" },
      { key: "inject_expectation_status", label: "Inject expectation status", kind: "chip" },
      { key: "base_created_at", label: "Created at", kind: "datetime" },
    ],
    rows: [
      {
        base_representative: "Detect beacon",
        inject_expectation_status: { label: "Not Detected", color: "danger", icon: "radio" },
        base_created_at: "2026-10-02T09:00:00Z",
        _url: "https://aev.example/admin/simulations/s1/injects/i1?expectation_id=e1",
        _icon: "radio",
        _entityType: "expectation-inject",
      },
      { base_representative: "Q4 drill", inject_expectation_status: null, base_created_at: null, _entityType: "simulation" },
    ],
    total: 2,
  },
});
const CTI_BOOKMARKS = ok<Chart>({
  shape: "rows",
  data: {
    columns: [
      { key: "name", label: "Name" },
      { key: "entity_type", label: "Type", kind: "chip" },
      { key: "modified", label: "Updated", kind: "date" },
    ],
    rows: ["Emotet", "APT28", "Q3", "Lazarus"].map((name, index) => ({
      name,
      entity_type: { label: "Malware" },
      modified: "2026-10-20T08:00:00Z",
      _icon: "biohazard",
      _url: `https://cti.example/dashboard/id/b${index}`,
    })),
    total: 4,
  },
});
const CTI_DISTRIBUTION = ok<Chart>({
  shape: "distribution",
  data: {
    items: [
      { label: "ACME", value: 1500, color: "#BA88FF", entityType: "Organization", icon: "building-2", link: "https://cti.example/dashboard/id/o1" },
      { label: "Intrusion Set", value: 12, color: "#41d613", entityType: "Intrusion-Set", icon: "circle" },
    ],
  },
});
const CTI_TIMELINE = ok<Chart>({
  shape: "rows",
  data: {
    columns: [
      { key: "date", label: "Date", kind: "datetime" },
      { key: "name", label: "Name" },
      { key: "secondary", label: "Description" },
    ],
    rows: [
      { date: "2026-10-02T08:00:00Z", name: "APT28", secondary: "**Russian** group " + "x".repeat(200), _url: "https://cti.example/dashboard/id/a", _icon: "gem", _color: "#FF9800" },
      { date: "2026-10-01T08:00:00Z", name: "Q3 report", secondary: null, _icon: "file-text", _color: "#70B23B" },
    ],
    total: 2,
  },
});
const CTI_MAP = ok<Ok & WidgetStudioData>({
  shape: "geo",
  data: {
    points: [{ label: "Lyon", lat: 45.76, lon: 4.84, value: 2, group: "City" }],
    regions: [
      { code: "FR", label: "France", value: 9, level: 6 },
      { code: "DE", label: "Germany", value: 1, level: 0 },
    ],
  },
});
const MAP_PRESET = {
  layers: ["choropleth", "marker"], regionColors: "level", basemap: "contrast", markerIcon: "building-2", markerColor: "#d95f00",
  markerSize: 25, zoom: 1, centerLat: 48.8566969, centerLng: 2.3514616, centerOnFirstMarker: true,
}; // prettier-ignore

// ── number ───────────────────────────────────────────────────────────────────

describe("number", () => {
  it("draws the difference style: the change over 24 hours with its arrow, the figure under it", () => {
    chart(CTI_NUMBER, "number", { valueStyle: "difference" });
    const value = document.querySelector("[data-value-style=difference]") as HTMLElement;
    expect(within(value).getByText("1.5K")).toBeTruthy();
    const change = value.querySelector("[data-direction]") as HTMLElement;
    expect(change.dataset.direction).toBe("up");
    expect(change.getAttribute("style")).toContain("var(--color-feedback-success-primary)");
    expect(change.textContent).toBe("3(24 hours)");
  });

  it.each([
    [-2, "down", "var(--color-feedback-error-primary)", "-2"],
    [0, "flat", "var(--text-default-secondary)", "0"],
  ])("draws a change of %s as %s", (change, direction, color, text) => {
    chart(ok<Chart>({ shape: "number", data: { value: 10, change, previous: 10 - change, period: "day" } }), "number", { valueStyle: "difference" });
    const shown = document.querySelector("[data-direction]") as HTMLElement;
    expect([shown.dataset.direction, shown.textContent]).toEqual([direction, `${text}(24 hours)`]);
    expect(shown.getAttribute("style")).toContain(color);
  });

  it("draws the tile style: the entity's icon tile, the figure counting up, the trend chip and its previous value", async () => {
    const onSelect = vi.fn();
    chart(AEV_NUMBER, "number", { valueStyle: "tile" }, onSelect);
    const tile = document.querySelector("[data-tile-icon]") as HTMLElement;
    expect(tile.dataset.tileIcon).toBe("zap");
    // (happy-dom drops a `color-mix` background; its 10% tint is `tileStyle`'s, checked on node --test.)
    expect(tile.getAttribute("style")).toContain("inset 0 0 14px color-mix(in srgb, var(--color-feedback-warning-primary) 13%, transparent)");
    const value = document.querySelector("[data-value]") as HTMLElement;
    await waitFor(() => expect(value.textContent).toBe("1.5K"));
    expect(value.getAttribute("title")).toBe("1,500");
    const trend = screen.getByText("+100").closest("[data-direction]") as HTMLElement;
    expect(trend.dataset.direction).toBe("up");
    expect(trend.getAttribute("title")).toBe("(1,400 previously)");
    fireEvent.click(value);
    expect(onSelect).toHaveBeenCalledWith({ name: "value", value: "1500" });
  });

  it.each([
    [{ change: 3, previous: 1, subject: "finding", span: "3m" }, "+3 findings over the last 3 months (1 previously)"],
    [{ change: -1, previous: 2, subject: "simulation", span: "1d" }, "-1 simulation over the last 24 hours (2 previously)"],
    [{ change: 4, previous: 0, subject: "inject", span: "all" }, "+4 injects since the beginning"],
    [{ change: 2, previous: 5, subject: "scenario" }, "+2 scenarios (5 previously)"],
  ])("says the tile's change in a sentence behind an info sign: %j", (extra, sentence) => {
    chart(ok<Chart>({ shape: "number", data: { value: 4, period: "interval", ...extra } as never }), "number", { valueStyle: "tile" });
    expect((document.querySelector("[data-value-info]") as HTMLElement).getAttribute("aria-label")).toBe(sentence);
  });

  it("has no info sign for a change it cannot name", () => {
    chart(AEV_NUMBER, "number", { valueStyle: "tile" });
    expect(document.querySelector("[data-value-info]")).toBeNull();
  });

  it("keeps XTM One's single value without a style", () => {
    chart(CTI_NUMBER, "number", {});
    expect(screen.getByTestId("single-value")).toBeTruthy();
    expect(document.querySelector("[data-value-style]")).toBeNull();
  });
});

// ── table / list / cards ─────────────────────────────────────────────────────

describe("list", () => {
  it("draws the table style: a header, typed cells, coloured chips and an arrow on the rows that open", () => {
    chart(CTI_LIST, "list", { listStyle: "table", rowHeight: 48, arrowColor: "accent" });
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Type", "Name", "Original creation date", "Labels", "Marking"]);
    const link = screen.getByRole("link", { name: "Emotet" });
    expect([link.getAttribute("href"), link.getAttribute("target"), link.getAttribute("style")]).toEqual([
      "https://cti.example/dashboard/id/m1",
      "_blank",
      "height: 48px;",
    ]);
    expect(within(link).getByText("Malware")).toBeTruthy();
    expect(within(link).getByText(written("2026-09-30T08:00:00Z", "date"))).toBeTruthy();
    expect([...link.querySelectorAll("[data-chip-color]")].map((chip) => chip.getAttribute("data-chip-color"))).toEqual([
      "#F0B60A", "#ff0000", "alert",
    ]); // prettier-ignore
    expect(within(link).getByText("ransomware")).toBeTruthy();
    expect(link.querySelector("[data-row-arrow]")).toBeTruthy();
    // The arrow in the colour the look names (OpenCTI's tertiary button, in the accent).
    expect((link.querySelector("[data-row-arrow]")!.parentElement as HTMLElement).style.color).toBe("var(--color-accent)");
    // A row without a link opens nothing and has no arrow; its empty cells read "-".
    const plain = screen.getByText("Q3").closest("[data-row]") as HTMLElement;
    expect(plain.tagName).toBe("DIV");
    expect(plain.querySelector("[data-row-arrow]")).toBeNull();
    expect(within(plain).getAllByText("-")).toHaveLength(3);
    expect(screen.getByText("2 of 12")).toBeTruthy();
  });

  it("draws the rows style: a leading icon (none on a row without one), typed cells, an arrow on the rows that open", () => {
    chart(AEV_LIST, "list", { listStyle: "rows", rowHeight: 50 });
    const link = screen.getByRole("link", { name: "Detect beacon" });
    expect(link.getAttribute("href")).toBe("https://aev.example/admin/simulations/s1/injects/i1?expectation_id=e1");
    expect(link.querySelector("[data-row-icon]")?.getAttribute("data-row-icon")).toBe("radio");
    expect(within(link).getByText("Not Detected")).toBeTruthy();
    expect(link.querySelector("[data-chip-color]")?.getAttribute("data-chip-color")).toBe("danger");
    expect(within(link).getByText(written("2026-10-02T09:00:00Z", "dateTime"))).toBeTruthy();
    expect(link.querySelector("[data-row-arrow]")).toBeTruthy();
    const simulation = screen.getByText("Q4 drill").closest("[data-row]") as HTMLElement;
    expect(simulation.querySelector("[data-row-icon]")).toBeNull();
    expect(simulation.querySelector("[data-row-arrow]")).toBeNull();
    expect(document.querySelector("[role=columnheader]")).toBeNull();
  });

  it("draws the cards style: three a row, each an avatar, its name and when it was updated", () => {
    chart(CTI_BOOKMARKS, "list", { listStyle: "cards", cardsPerRow: 3 });
    const grids = document.querySelectorAll("[data-list-style=cards] > div");
    expect([...grids].map((grid) => grid.children.length)).toEqual([3, 1]);
    expect((grids[0] as HTMLElement).style.gridTemplateColumns).toBe("repeat(3, minmax(0, 1fr))");
    const card = screen.getByRole("link", { name: "Emotet" });
    expect(card.getAttribute("href")).toBe("https://cti.example/dashboard/id/b0");
    expect(within(card).getByText(`Updated on ${written("2026-10-20T08:00:00Z", "date")}`)).toBeTruthy();
    expect(card.querySelector("[data-card-avatar]")?.getAttribute("data-card-avatar")).toBe("biohazard");
  });

  it("hands a clicked row to the dashboard's interactions instead of opening it", () => {
    const onSelect = vi.fn();
    chart(CTI_LIST, "list", { listStyle: "table" }, onSelect);
    expect(screen.queryByRole("link")).toBeNull();
    fireEvent.click(screen.getByText("Emotet").closest("[data-row]") as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ name: "entity_type", value: "Malware", row: expect.objectContaining({ name: "Emotet" }) }));
  });

  it("never links a row to an address that is not the web", () => {
    const evil = ok<Chart>({ shape: "rows", data: { columns: [{ key: "name", label: "Name" }], rows: [{ name: "x", _url: "javascript:alert(1)" }], total: null } });
    chart(evil, "list", { listStyle: "rows" });
    expect(screen.queryByRole("link")).toBeNull();
  });
});

// ── distribution list ────────────────────────────────────────────────────────

describe("distribution list", () => {
  it("draws rows: the item's icon in its colour, its label, its value in SI form in the accent, linked when it has a link", () => {
    chart(CTI_DISTRIBUTION, "distribution", { listStyle: "rows" });
    const acme = screen.getByRole("link", { name: /ACME/ });
    expect(acme.getAttribute("href")).toBe("https://cti.example/dashboard/id/o1");
    expect(within(acme).getByText("1.5K")).toBeTruthy();
    const icon = acme.querySelector("[data-item-icon]") as HTMLElement;
    expect([icon.dataset.itemIcon, icon.style.color.toLowerCase()]).toEqual(["building-2", "#ba88ff"]);
    const type = screen.getByText("Intrusion Set").closest("span[class*=cursor-default]");
    expect(type).toBeTruthy();
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("keeps XTM One's bars without a style", () => {
    chart(CTI_DISTRIBUTION, "distribution", {});
    expect(document.querySelector("[data-distribution-style=bars]")).toBeTruthy();
  });
});

// ── timeline ─────────────────────────────────────────────────────────────────

describe("timeline", () => {
  it("draws the alternate style: sides swapping, dots in the entity's colour opening its page, the second line cut", () => {
    studio(CTI_TIMELINE, "timeline", { timelineStyle: "alternate" });
    const entries = document.querySelectorAll("[data-entry]");
    expect(entries).toHaveLength(2);
    expect([...document.querySelectorAll("[data-card-side]")].map((c) => c.getAttribute("data-card-side"))).toEqual(["right", "left"]);
    const dot = screen.getByRole("link", { name: "APT28" });
    expect(dot.getAttribute("href")).toBe("https://cti.example/dashboard/id/a");
    expect(dot.querySelector("[data-dot-color]")?.getAttribute("data-dot-color")).toBe("#FF9800");
    expect(screen.getByText("Russian").tagName).toBe("STRONG");
    expect(entries[0].textContent).toContain("…");
    expect(entries[0].textContent).toContain(written("2026-10-02T08:00:00Z", "longDateTime"));
    // An entry without a link has no link on its dot.
    expect(screen.queryByRole("link", { name: "Q3 report" })).toBeNull();
  });
});

// ── map ──────────────────────────────────────────────────────────────────────

describe("map", () => {
  it("colours each region by its level on the threat scale, thin and outlined, with city markers", async () => {
    studio(CTI_MAP, "map", MAP_PRESET);
    const svg = (await screen.findByRole("img", { name: /Map of/ }, { timeout: 15_000 })) as unknown as SVGElement;
    expect([svg.dataset.basemap, svg.dataset.regionColors]).toEqual(["contrast", "level"]);
    expect(svg.querySelector("[data-sea]")?.getAttribute("style")).toContain("#0b1422");
    const france = svg.querySelector('[data-code="FR"][data-valued]') as SVGElement;
    expect(france.dataset.level).toBe("6");
    expect(france.getAttribute("style")).toMatch(/fill: #e64a19; fill-opacity: 0\.1; stroke: #e64a19; stroke-width: 1/);
    // Level 0 is drawn as level 5.
    expect(svg.querySelector('[data-code="DE"][data-valued]')?.getAttribute("style")).toContain("fill: #d95f00");
    const markers = document.querySelectorAll("[data-marker-icon]");
    expect(markers).toHaveLength(1);
    expect((markers[0] as HTMLElement).style.width).toBe("25px");
    expect((markers[0] as HTMLElement).title).toBe("Lyon: 2");
    // No legend of classes on a threat map.
    expect(document.querySelectorAll("[data-viz=map] [aria-hidden=true] span.flex-1")).toHaveLength(0);
    // Each levelled region's outline is drawn again above its neighbours' borders.
    expect([...svg.querySelectorAll("[data-outline]")].map((o) => o.getAttribute("data-outline")).sort()).toEqual(["DE", "FR"]);
  }, 20_000);

  it("keeps XTM One's classes without the threat look", async () => {
    studio(CTI_MAP, "map", {});
    const svg = (await screen.findByRole("img", { name: /Map of/ }, { timeout: 15_000 })) as unknown as SVGElement;
    expect(svg.dataset.regionColors).toBe("classes");
    expect(document.querySelector("[data-marker-icon]")).toBeNull();
  }, 20_000);
});

// ── empty and sample ─────────────────────────────────────────────────────────

describe("empty and sample data", () => {
  it.each([
    ["list", ok<Chart>({ shape: "rows", data: { columns: [{ key: "name", label: "Name" }], rows: [], total: 0 } }), { listStyle: "table" }],
    ["distribution", ok<Chart>({ shape: "distribution", data: { items: [] } }), { listStyle: "rows" }],
  ] as const)("says a %s has no data", (view, data, options) => {
    chart(data, view, options);
    expect(screen.getByText("No data for this period.")).toBeTruthy();
  });

  it("says a timeline has no events", () => {
    studio(ok({ shape: "events", data: { events: [] } }), "timeline", { timelineStyle: "alternate" });
    expect(screen.getByText("No data for this period.")).toBeTruthy();
  });

  it.each([
    ["xtm.table", "list", { listStyle: "table" }],
    ["xtm.table", "list", { listStyle: "rows" }],
    ["xtm.table", "list", { listStyle: "cards" }],
    ["xtm.pie", "distribution", { listStyle: "rows" }],
    ["xtm.singlevalue", "number", { valueStyle: "difference" }],
    ["xtm.singlevalue", "number", { valueStyle: "tile" }],
  ] as const)("draws a new chart's sample data (%s) in the style %o", (type, view, options) => {
    const added = addChart({ visualizations: {}, dataSources: {}, layout: { type: "grid", structure: [] } }, type, null, "Sample data");
    const source = Object.values((added?.definition.dataSources ?? {}) as Record<string, { options: { shape: string; data: unknown } }>)[0];
    const { container } = chart(ok<Chart>({ shape: source.options.shape, data: source.options.data } as Chart), view, options);
    expect(container.textContent).not.toContain("No data for this period.");
    expect(container.querySelector("[data-list-style], [data-distribution-style], [data-value-style]")).toBeTruthy();
  });
});

// ── a live widget: presets, markdown, errors ─────────────────────────────────

const ID = "11111111-1111-4111-8111-111111111111";
const INTEGRATION = "22222222-2222-4222-8222-222222222222";

function board(widgets: Record<string, unknown>[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(<CustomDashboardView dashboard={normalizeDashboard({ title: "Board", widgets })} dashboardId={ID} revision={1} />, client);
}
const live = (source: string, native: Record<string, unknown>, view: string) => ({
  kind: "platform", id: "live", title: "Live", source, integration_id: INTEGRATION, view, native,
}); // prettier-ignore

describe("a live widget", () => {
  beforeEach(() => api.post.mockReset());

  it("draws its module's look from the preset its answer carries", async () => {
    api.post.mockResolvedValue({ ...AEV_LIST, presentation: { view: "xtm.list", options: { listStyle: "rows", rowHeight: 50 } } });
    board([live("openaev", { widget_type: "list", widget_config: {} }, "list")]);
    expect(await screen.findByRole("link", { name: "Detect beacon" }, { timeout: 15_000 })).toBeTruthy();
    expect(document.querySelector("[data-list-style=rows]")).toBeTruthy();
  }, 20_000);

  it("keeps XTM One's look when drawn with another view than the preset's", async () => {
    api.post.mockResolvedValue({ ...CTI_LIST, presentation: { view: "xtm.list", options: { listStyle: "table" } } });
    board([live("opencti", { type: "list", perspective: "entities", dataSelection: [{}] }, "table")]);
    expect(await screen.findByText("Emotet", {}, { timeout: 15_000 })).toBeTruthy();
    expect(document.querySelector("[data-list-style]")).toBeNull();
  }, 20_000);

  it.each([
    ["opencti", { type: "list", perspective: "entities", dataSelection: [{}] }, "OpenCTI could not answer."],
    ["openaev", { widget_type: "list", widget_config: {} }, "OpenAEV could not answer."],
  ])("says why a %s list has no data", async (source, native, sentence) => {
    api.post.mockResolvedValue({ status: "error", code: "upstream_error", open_url: null } satisfies WidgetData);
    board([live(source, native, "list")]);
    expect(await screen.findByText(sentence)).toBeTruthy();
  });

  it("draws a Markdown text: headings, tables, safe links only, no HTML", () => {
    board([
      {
        kind: "text",
        id: "md",
        format: "markdown",
        body: "# Weekly brief\n\n| Actor | Reports |\n|---|---|\n| APT28 | 4 |\n\n[report](https://cti.example/r) [bad](javascript:alert(1)) <b>raw</b>",
      },
    ]);
    const markdown = document.querySelector("[data-markdown]") as HTMLElement;
    expect(within(markdown).getByRole("heading", { level: 1, name: "Weekly brief" })).toBeTruthy();
    expect(within(markdown).getByRole("cell", { name: "APT28" })).toBeTruthy();
    expect(within(markdown).getByRole("link", { name: "report" }).getAttribute("href")).toBe("https://cti.example/r");
    expect(within(markdown).queryByRole("link", { name: "bad" })).toBeNull();
    expect(markdown.querySelector("b")).toBeNull();
  });

  it("keeps a plain text's paragraphs", () => {
    board([{ kind: "text", id: "p", body: "# not a heading" }]);
    expect(document.querySelector("[data-markdown]")).toBeNull();
    expect(screen.getByText("# not a heading")).toBeTruthy();
  });
});
