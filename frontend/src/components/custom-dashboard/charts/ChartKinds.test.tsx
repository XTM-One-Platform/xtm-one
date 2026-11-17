// The chart kinds as a viewer meets them, drawn from each module's recorded
// answer: `backend/tests/fixtures/dashboards/module_charts.json` holds, for a
// widget of every chart type of OpenCTI and OpenAEV, the closed data and the
// presentation preset their adapters make (`backend/tests/test_dashboard_live_charts.py`
// pins the same records). The preset is laid under the options as a dashboard
// does (`definition.presentedOptions`); what is asserted is what the module's
// own widget shows (`dev-docs/dashboard-rendering-reference.md`): labels,
// colours, counts, formats, the sample and empty states, what a click says.
// The charts measure their container, which happy-dom does not lay out: the
// container is given a size here.

import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetChartData, WidgetData, WidgetTreeData } from "@/types";

import { presentedOptions } from "../definition";
import type { PlatformView } from "../spec";
import { distributionToTree } from "../viz/shapes";
import PlatformChart from "./PlatformChart";
import { TreemapViz } from "./TreemapViz";

const WIDTH = 600;
const HEIGHT = 260;

vi.mock("recharts", async (original) => {
  const actual = await original<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children) ? cloneElement(children as ReactElement<{ width?: number; height?: number }>, { width: WIDTH, height: HEIGHT }) : null,
  };
});

beforeAll(() => {
  // The SVG drawings measure their box with a ResizeObserver: it reports the container's size at once.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(private readonly callback: (entries: { contentRect: { width: number; height: number } }[]) => void) {}
      observe() {
        this.callback([{ contentRect: { width: WIDTH, height: HEIGHT } }]);
      }
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  delete document.documentElement.dataset.mode;
  vi.restoreAllMocks();
});

type Ok = Extract<WidgetData, { status: "ok" }> & WidgetChartData;
interface Case {
  name: string;
  source: string;
  shape: "series" | "distribution";
  view: PlatformView;
  data: unknown;
  presentation: { view: string; options: Record<string, unknown> };
}

const FIXTURE = JSON.parse(readFileSync(path.resolve(__dirname, "../../../../../backend/tests/fixtures/dashboards/module_charts.json"), "utf8")) as { cases: Case[] };
const CASE = (name: string): Case => {
  const found = FIXTURE.cases.find((c) => c.name === name);
  if (!found) throw new Error(`no case ${name}`);
  return found;
};

function draw(name: string, overrides: { data?: unknown; onSelect?: (event: unknown) => void } = {}) {
  const c = CASE(name);
  const data = { status: "ok", fetched_at: "2026-10-26T10:00:00Z", open_url: null, shape: c.shape, data: overrides.data ?? c.data, presentation: c.presentation } as Ok;
  const options = presentedOptions({}, c.view, c.presentation);
  return renderWithProviders(<PlatformChart data={data} view={c.view} height={HEIGHT} options={options} onSelect={overrides.onSelect} />);
}

const attr = (nodes: Iterable<Element>, name: string) => [...nodes].map((node) => node.getAttribute(name));
const texts = (nodes: Iterable<Element>) => [...nodes].map((node) => node.textContent);

describe("lineArea", () => {
  it("draws OpenCTI's lines: the accent then Material 400, months on a category axis, SI numbers with a space, the legend", () => {
    const { container } = draw("opencti-line");
    expect(attr(container.querySelectorAll(".recharts-line-curve"), "stroke")).toEqual(["var(--color-accent)", "#ef5350"]);
    expect(attr(container.querySelectorAll(".recharts-line-curve"), "stroke-width")).toEqual(["2", "2"]);
    // No markers.
    expect(container.querySelectorAll(".recharts-line-dots circle")).toHaveLength(0);
    const xTicks = texts(container.querySelectorAll(".recharts-xAxis-tick-labels text"));
    expect(xTicks[0]).toBe("May 2026");
    expect(texts(container.querySelectorAll(".recharts-yAxis-tick-labels text")).some((t) => / K$/.test(t ?? ""))).toBe(true);
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["Reports", "Malware"]);
    // No axis line; dashed horizontal grid lines in the text colour at 0.1.
    expect(container.querySelector(".recharts-cartesian-axis-line")).toBeNull();
    expect(container.querySelectorAll(".recharts-cartesian-grid-vertical line")).toHaveLength(0);
    expect(container.querySelector(".recharts-cartesian-grid-horizontal line")?.getAttribute("stroke")).toBe("rgba(255, 255, 255, 0.1)");
  });

  it("draws OpenCTI's area with its 0.7 to 0.1 gradient, days on a time axis", () => {
    const { container } = draw("opencti-area");
    const stops = container.querySelectorAll("linearGradient stop");
    expect(attr(stops, "stop-opacity")).toEqual(["0.7", "0.1"]);
    expect(container.querySelector(".recharts-area-area")?.getAttribute("fill")).toMatch(/^url\(#.+-s0\)$/);
    const ticks = texts(container.querySelectorAll(".recharts-xAxis-tick-labels text"));
    expect(ticks[0]).toBe("Oct 1, 2026");
    expect(ticks.length).toBeLessThanOrEqual(5);
  });

  it("draws OpenAEV's line as areas under smooth lines: Material colours, a 0.35 to 0.02 gradient, 4 px markers, long dates", () => {
    const { container } = draw("openaev-line");
    expect(attr(container.querySelectorAll(".recharts-area-curve"), "stroke")).toEqual(["#ef5350", "#ec407a"]);
    expect(attr(container.querySelectorAll("linearGradient stop"), "stop-opacity")).toEqual(["0.35", "0.02", "0.35", "0.02"]);
    expect(new Set(attr(container.querySelectorAll(".recharts-area-dots circle"), "r"))).toEqual(new Set(["4"]));
    expect(texts(container.querySelectorAll(".recharts-xAxis-tick-labels text"))[0]).toBe("September 7, 2026");
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["Findings", "Injects"]);
  });

  it("draws OpenAEV's sample, greyed and marked, while its series are empty", () => {
    const empty = { series: [{ name: "Findings", points: [] }] };
    const { container } = draw("openaev-line", { data: empty });
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(container.querySelector("[data-sample]")?.getAttribute("data-sample")).toBe("full");
    expect(container.querySelectorAll(".recharts-area-dots circle")).toHaveLength(8);
  });

  it("says there is no data as the module says it, in the middle of the whole chart", () => {
    draw("opencti-line", { data: { series: [] } });
    const note = screen.getByText("No data has been found.");
    expect(note.style.height).toBe(`${HEIGHT}px`);
  });

  it("reads a series of zeros 0 to 2, by halves", () => {
    const zeros = { series: [{ name: "Reports", points: [{ x: "2026-09-01", y: 0 }, { x: "2026-10-01", y: 0 }] }] };
    const { container } = draw("opencti-line", { data: zeros });
    expect(texts(container.querySelectorAll(".recharts-yAxis-tick-labels text"))).toEqual(["0", "0.5", "1", "1.5", "2"]);
  });
});

describe("bars", () => {
  it("draws OpenCTI's columns in the accent, their ends rounded", () => {
    const { container } = draw("opencti-vertical-bar");
    const bars = container.querySelectorAll(".recharts-bar-rectangle path");
    expect(bars).toHaveLength(3);
    expect(new Set(attr(bars, "fill"))).toEqual(new Set(["var(--color-accent)"]));
    // Rounded ends: a path with arcs, not a plain rectangle.
    expect(bars[0].getAttribute("d")).toContain("A 4,4");
  });

  it("draws OpenCTI's horizontal bars in their entities' colours, a click opening the entity", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { container } = draw("opencti-horizontal-bar");
    const bars = container.querySelectorAll(".recharts-bar-rectangle path");
    expect(attr(bars, "fill")).toEqual(["#BA88FF", "#BA88FF", "#424242"]);
    expect(texts(container.querySelectorAll(".recharts-yAxis-tick-labels text"))).toEqual(["Northwind CERT", "Jane Analyst", "Restricted"]);
    // The value axis has two labels: zero and the next round value past the top.
    expect(container.querySelectorAll(".recharts-xAxis-tick-labels text")).toHaveLength(2);
    // Each label on one line, and a grid line between the categories (and at both ends).
    expect(container.querySelectorAll(".recharts-yAxis-tick-labels tspan")).toHaveLength(0);
    expect(container.querySelectorAll(".recharts-cartesian-grid-horizontal line")).toHaveLength(4);
    fireEvent.click(container.querySelector(".recharts-wrapper") as Element, { clientX: 300, clientY: 40 });
    expect(open.mock.calls).toEqual([["https://cti.example.test/dashboard/id/4c0d9e7a-1111-4f51-9f3e-000000000001", "_blank", "noopener,noreferrer"]]);
  });

  it("draws OpenAEV's columns by meaning, in a 0.95 to 0.65 gradient", () => {
    const { container } = draw("openaev-vertical-bar-temporal");
    const gradients = container.querySelectorAll("linearGradient");
    expect(attr([...gradients].map((g) => g.querySelector("stop") as Element), "stop-color")).toEqual(["#f14337", "var(--color-feedback-success-primary)"]);
    expect(attr(container.querySelectorAll("linearGradient stop"), "stop-opacity").slice(0, 2)).toEqual(["0.95", "0.65"]);
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["Not Detected", "Prevented"]);
  });

  it("draws an OpenAEV breakdown in its series' colour, a gradient per bar", () => {
    const { container } = draw("openaev-vertical-bar-structural");
    const bars = container.querySelectorAll(".recharts-bar-rectangle path");
    expect(bars).toHaveLength(3);
    expect(attr(bars, "fill").every((fill) => /^url\(#.+-p\d\)$/.test(fill ?? ""))).toBe(true);
    expect(new Set(attr(container.querySelectorAll("linearGradient[id$='-p0'] stop, linearGradient[id$='-p1'] stop"), "stop-color"))).toEqual(new Set(["#f14337"]));
  });

  it("draws OpenAEV's horizontal bars: a colour per bar (the accent first), values at their ends, labels cut at 28", () => {
    const { container } = draw("openaev-horizontal-bar");
    const stops = [...container.querySelectorAll("linearGradient[id*='-p']")].map((g) => g.querySelector("stop")?.getAttribute("stop-color"));
    expect(stops).toEqual(["var(--color-accent)", "#ef5350", "#ec407a"]);
    expect(texts(container.querySelectorAll(".recharts-yAxis-tick-labels text"))[0]).toBe("T1059 Command and Scripting…");
    const value = screen.getByText("1,532");
    expect([value.getAttribute("font-weight"), value.getAttribute("font-size")]).toEqual(["600", "12"]);
    // Vertical grid lines only.
    expect(container.querySelectorAll(".recharts-cartesian-grid-horizontal line")).toHaveLength(0);
  });

  it("draws OpenAEV's technique sample while the breakdown has only zeros", () => {
    draw("openaev-horizontal-bar", { data: { items: [{ label: "T1059", value: 0 }] } });
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(screen.getByText("T1059 Command and Scripting…")).toBeTruthy();
  });
});

describe("samples", () => {
  it("draws OpenAEV's TTP sample as its two series side by side, with their legend", () => {
    const { container } = draw("openaev-horizontal-bar", { data: { items: [] } });
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(container.querySelectorAll(".recharts-bar")).toHaveLength(2);
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["Detected TTPs", "Prevented TTPs"]);
  });
});

describe("donut", () => {
  it("draws OpenCTI's labels in their own colours, shares on the ring, a 3 px separator", () => {
    const { container } = draw("opencti-donut-labels");
    const sectors = container.querySelectorAll(".recharts-pie-sector path");
    // A label without a colour takes the palette's (OpenCTI shifts the colours: a defect drawn as meant).
    expect(attr(sectors, "fill")).toEqual(["#ff0000", "#ffffff", "#ec407a"]);
    expect(new Set(attr(sectors, "stroke-width"))).toEqual(new Set(["3"]));
    expect(screen.getByText("55.6%").getAttribute("font-size")).toBe("10");
    // On the white slice, the share is black.
    expect(screen.getByText("31.1%").getAttribute("fill")).toBe("#000000");
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["phishing", "tlp-clear", "apt"]);
  });

  it("sets OpenCTI's donut at the top, as large as the room above its legend, the legend's dots round and in the text colour", () => {
    const { container } = draw("opencti-donut-labels");
    const sector = container.querySelector(".recharts-pie-sector path") as Element;
    // 260 px less the legend's 32: a circle of 228 px, its centre 114 px down, in the middle of the width.
    const size = Math.min(WIDTH, HEIGHT - 32);
    expect(Number(sector.getAttribute("cy"))).toBe(size / 2);
    expect(Number(sector.getAttribute("cx"))).toBe(WIDTH / 2);
    // The ring's outer arc: the radius of the circle that fits, a little inside its edge.
    expect(Number(/A\s*([\d.]+),/.exec(sector.getAttribute("d") ?? "")?.[1])).toBeCloseTo(size / 2.05);
    expect(container.querySelector(".recharts-legend-item path.recharts-symbols")?.getAttribute("d")).toContain("A");
    expect((container.querySelector(".recharts-legend-item-text span") as HTMLElement).style.color).toBe("var(--text-default-primary)");
  });

  it("draws true and false in Material green and red, the light theme's 600 tint in the light theme", () => {
    const { container, unmount } = draw("opencti-donut-boolean");
    expect(attr(container.querySelectorAll(".recharts-pie-sector path"), "fill")).toEqual(["#66bb6a", "#ef5350"]);
    unmount();
    document.documentElement.dataset.mode = "light";
    const light = draw("opencti-donut-boolean");
    expect(attr(light.container.querySelectorAll(".recharts-pie-sector path"), "fill")).toEqual(["#43a047", "#e53935"]);
  });

  it("draws OpenAEV's statuses in their colours and says which was clicked by its key", () => {
    const onSelect = vi.fn();
    const { container } = draw("openaev-donut-status", { onSelect });
    const sectors = container.querySelectorAll(".recharts-pie-sector path");
    expect(attr(sectors, "fill")).toEqual(["var(--color-feedback-success-primary)", "#f14337", "#9e9e9e"]);
    fireEvent.click(sectors[1]);
    expect(onSelect.mock.calls).toEqual([[{ name: "FAILED", value: "FAILED", y: "27" }]]);
  });

  it("draws OpenAEV's status sample when the breakdown is empty", () => {
    const { container } = draw("openaev-donut-status", { data: { items: [] } });
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["SUCCESS", "FAILED", "PENDING"]);
  });
});

describe("radar", () => {
  it("draws OpenCTI's radar: one accent shape, a 1 px line, 5 px markers, labels cut at 25 on a word", () => {
    const { container } = draw("opencti-radar");
    const shape = container.querySelector(".recharts-radar-polygon path");
    expect([shape?.getAttribute("stroke"), shape?.getAttribute("stroke-width"), shape?.getAttribute("fill-opacity")]).toEqual(["var(--color-accent)", "1", "0.2"]);
    expect(new Set(attr(container.querySelectorAll(".recharts-radar-dots circle"), "r"))).toEqual(new Set(["5"]));
    // Solid markers: the shape's 0.2 fill does not reach them.
    expect(new Set(attr(container.querySelectorAll(".recharts-radar-dots circle"), "fill-opacity"))).toEqual(new Set(["1"]));
    expect(texts(container.querySelectorAll(".recharts-polar-angle-axis-tick-value"))).toContain("Vulnerability With A...");
    // One series: no legend.
    expect(container.querySelector(".recharts-legend-wrapper")).toBeNull();
  });

  it("draws OpenAEV's posture radar: a shape per outcome in its colour, 3 px markers, the legend below", () => {
    const { container } = draw("openaev-posture-radar");
    expect(attr(container.querySelectorAll(".recharts-radar-polygon path"), "stroke")).toEqual(["var(--color-feedback-success-primary)", "#f14337", "#f14337"]);
    expect(new Set(attr(container.querySelectorAll(".recharts-radar-polygon path"), "stroke-width"))).toEqual(new Set(["2"]));
    expect(new Set(attr(container.querySelectorAll(".recharts-radar-dots circle"), "r"))).toEqual(new Set(["3"]));
    expect(texts(container.querySelectorAll(".recharts-legend-item-text"))).toEqual(["Success", "Failed", "NOT DETECTED"]);
  });

  it("draws OpenAEV's radar sample in a lighter grey", () => {
    const { container } = draw("openaev-posture-radar", { data: { series: [] } });
    expect(container.querySelector("[data-sample]")?.getAttribute("data-sample")).toBe("subtle");
  });
});

describe("polarArea", () => {
  it("draws OpenCTI's polar area: Material slices at 0.5 on round rings, a label-only tooltip, a click", () => {
    const onSelect = vi.fn();
    const { container } = draw("opencti-polar-area", { onSelect });
    const slices = container.querySelectorAll("svg[aria-label='Polar area chart'] path");
    expect(attr(slices, "fill")).toEqual(["#ef5350", "#ab47bc", "#ec407a", "#7e57c2"]);
    expect(new Set(attr(slices, "fill-opacity"))).toEqual(new Set(["0.5"]));
    expect(attr(container.querySelectorAll("circle[data-ring]"), "data-ring")).toEqual(["10", "20", "30", "40"]);
    expect(container.querySelectorAll("svg[aria-label='Polar area chart'] line")).toHaveLength(4);
    fireEvent.mouseMove(slices[2]);
    expect(screen.getByRole("tooltip").textContent).toBe("Attack Pattern");
    // The label alone: no value in a native tooltip either.
    expect(slices[2].querySelector("title")).toBeNull();
    fireEvent.click(slices[1]);
    expect(onSelect.mock.calls).toEqual([[{ name: "Intrusion Set", value: "Intrusion-Set", y: "22" }]]);
  });
});

describe("heatmap", () => {
  it("draws OpenCTI's heatmap: the first series at the bottom, ten ranges of blues, zero apart, the ranges as the legend", () => {
    const { container } = draw("opencti-heatmap");
    expect(attr(container.querySelectorAll("[data-row]"), "data-row")).toEqual(["Notes", "Reports"]);
    const reports = [...container.querySelectorAll("[data-row='Reports'] rect")];
    expect(attr(reports, "fill")).toEqual(["#001e3c", "#023362", "#045198", "#2986e7", "#0561b4", "#02407a", "#9eccff"]);
    const legend = texts(container.querySelectorAll("[data-range]"));
    expect(legend).toHaveLength(11);
    expect([legend[0], legend[9], legend[10]]).toEqual(["1 - 4", "36 - 40", "0 - 0"]);
    expect(texts(container.querySelectorAll("svg[aria-label='Heatmap'] > text"))[0]).toBe("Oct 1, 2026");
  });
});

describe("wordCloud", () => {
  it("draws OpenCTI's word cloud: positive words, 20 to 50 px on a log scale, Material colours", () => {
    const onSelect = vi.fn();
    const { container } = draw("opencti-wordcloud", { onSelect });
    const words = [...container.querySelectorAll("svg[aria-label='Word cloud'] text")];
    expect(words.map((w) => [...w.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(""))).toEqual(["phishing", "ransomware", "apt", "botnet", "cve"]);
    expect(attr(words, "font-size")).toEqual(["50", "45", "38", "29", "20"]);
    expect(attr(words, "fill")).toEqual(["#ef5350", "#ab47bc", "#ec407a", "#7e57c2", "#5c6bc0"]);
    expect(words[0].querySelector("title")?.textContent).toBe("phishing (90)");
    fireEvent.click(words[2]);
    expect(onSelect.mock.calls).toEqual([[{ name: "apt", value: "apt", y: "30" }]]);
  });
});

describe("treemap", () => {
  it("draws OpenCTI's distributed tree: a Material colour per cell, a 3 px separator, the name centred", () => {
    const c = CASE("opencti-tree");
    const tree = distributionToTree(c.data as never) as WidgetTreeData;
    const { container } = renderWithProviders(<TreemapViz data={tree} width={WIDTH} height={HEIGHT} options={presentedOptions({}, c.view, c.presentation)} />);
    const cells = [...container.querySelectorAll("[data-cell] rect")];
    expect(cells.map((cell) => (cell as SVGElement).style.fill)).toEqual(["#ef5350", "#ab47bc", "#ec407a", "#7e57c2"]);
    expect(new Set(cells.map((cell) => (cell as SVGElement).style.strokeWidth))).toEqual(new Set(["3"]));
    expect(texts(container.querySelectorAll("[data-cell] text"))).toEqual(["Report", "Note", "Grouping", "Opinion"]);
    expect(cells[0].querySelector("title")?.textContent).toBe("Report: 120");
  });

  it("draws an undistributed tree in the accent, smaller values in darker shades", () => {
    const c = CASE("opencti-tree");
    const tree = distributionToTree(c.data as never) as WidgetTreeData;
    const options = { paletteLead: "accent", palette: "materialMixed", valueShades: true, separatorWidth: 3 };
    const { container } = renderWithProviders(<TreemapViz data={tree} width={WIDTH} height={HEIGHT} options={options} />);
    const fills = [...container.querySelectorAll("[data-cell] rect")].map((cell) => (cell as SVGElement).style.fill);
    expect(fills[0]).toBe("var(--color-accent)");
    expect(fills[3]).toMatch(/^color-mix\(in srgb, #000000 47%, var\(--color-accent\)\)$/);
  });
});
