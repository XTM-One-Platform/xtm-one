// The validation views as a viewer meets them, drawn from what the server
// adapter makes of OpenAEV's recorded answers (`backend/tests/
// test_dashboard_live_posture.py`): labels, colours, counts, the empty, sample
// and scope states, and what a click says or opens.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetChartData, WidgetData, WidgetOutcomesData } from "@/types";

import type { ClickEvent } from "../interactions";
import type { PlatformView } from "../spec";
import PlatformChart from "./PlatformChart";

type Ok = Extract<WidgetData, { status: "ok" }> & WidgetChartData;

const ok = (data: WidgetOutcomesData): Ok => ({ status: "ok", fetched_at: "2026-10-26T10:00:00Z", open_url: null, shape: "outcomes", data }) as Ok;

function draw(data: WidgetOutcomesData, view: PlatformView, options: Record<string, unknown> = {}, onSelect: Mock<(event: ClickEvent) => void> | null = vi.fn()) {
  renderWithProviders(<PlatformChart data={ok(data)} view={view} height={420} options={options} onSelect={onSelect ?? undefined} />);
  return onSelect;
}

const BASE = "https://cti.example";

// ── The adapter's answers ────────────────────────────────────────────────────

/** A command center: SUCCESS / FAILED / PENDING by expectation type, its platforms and calls to action. */
const CENTER: WidgetOutcomesData = {
  items: [
    { label: "Vulnerability", success: 23, failed: 17, pending: 0, parts: [], icon: "bug", color: "#0fbcff", key: "VULNERABILITY", successLabel: "not vulnerable" },
    { label: "Prevention", success: 58, failed: 22, pending: 0, parts: [], icon: "shield-check", color: "#0fbcff", key: "PREVENTION", successLabel: "prevented" },
    { label: "Detection", success: 44, failed: 38, pending: 9, parts: [], icon: "radio", color: "#0fbcff", key: "DETECTION", successLabel: "detected" },
    { label: "Custom probe", success: 2, failed: 0, pending: 0, parts: [], icon: "circle-question-mark", key: "CUSTOM_PROBE", successLabel: "stopped" },
  ],
  orbit: [
    { label: "CrowdStrike Falcon Insight XDR", type: "EDR", color: "#0fbcff" },
    { label: "Proofpoint", type: "EMAIL_SECURITY", color: "#4dd0e1" },
    { label: "Home made", type: "CUSTOM" },
  ],
  actions: [
    { label: "Design a scenario", caption: "Emulate real-world adversaries", icon: "route", color: "accent", link: `${BASE}/admin/scenarios` },
    { label: "Review findings", caption: "Hunt down validated gaps", icon: "binoculars", color: "danger", link: `${BASE}/admin/findings` },
  ],
}; // prettier-ignore

/** An exposure score's buckets, as they come. */
const SCORE: WidgetOutcomesData = { items: [CENTER.items[1], CENTER.items[2], CENTER.items[0], CENTER.items[3], { ...CENTER.items[0], label: "Manual", key: "MANUAL", success: 1, failed: 0 }] };

/** A resilience gauge on detection. */
const GAUGE: WidgetOutcomesData = { items: [{ label: "Detection", success: 64, failed: 27, pending: 0, parts: [], icon: "eye" }] };

/** The security domains: Endpoint measured, Cloud listed. */
const DOMAINS: WidgetOutcomesData = {
  items: [
    {
      label: "Endpoint", success: 33, failed: 10, pending: 2, icon: "monitor-smartphone",
      parts: [
        { label: "Prevention", success: 30, failed: 10, pending: 2, icon: "shield-check", key: "PREVENTION", total: 42,
          rows: [{ label: "Prevented", value: 30, status: "success", key: "SUCCESS" }, { label: "Not Prevented", value: 10, status: "failed", key: "FAILED" },
                 { label: "Pending", value: 2, status: "pending", key: "PENDING" }] },
        { label: "Manual", success: 3, failed: 0, pending: 0, icon: "circle-check-big", key: "MANUAL", total: 3, rows: [{ label: "Success", value: 3, status: "success", key: "SUCCESS" }] },
      ],
    },
  ],
  reference: [
    { id: "d-endpoint", label: "Endpoint", icon: "monitor-smartphone", parts: [{ label: "Prevention", icon: "shield-check" }] },
    { id: "d-cloud", label: "Cloud", icon: "cloud", parts: [{ label: "Prevention", icon: "shield-check" }, { label: "Detection", icon: "radio" }] },
  ],
}; // prettier-ignore

/** A security coverage: two kill chains, ATT&CK opening; Phishing tested, Valid Accounts present untested. */
const COVERAGE: WidgetOutcomesData = {
  chains: [
    { id: "mitre-atlas", label: "MITRE ATLAS", groups: [{ id: "recon", label: "Reconnaissance" }] },
    { id: "mitre-attack", label: "MITRE ATT&CK", default: true, groups: [{ id: "ta-init", label: "INITIAL ACCESS" }, { id: "ta-exec", label: "Execution" }] },
  ],
  reference: [
    { id: "ap-phish", label: "Phishing", code: "T1566", phases: ["ta-init"], subIds: ["ap-phish-link"] },
    { id: "ap-valid", label: "Valid Accounts", code: "T1078", phases: ["ta-init", "ta-exec"] },
    { id: "ap-cmd", label: "Command and Scripting Interpreter", code: "T1059", phases: ["ta-exec"] },
  ],
  items: [
    { label: "Phishing", success: 2, failed: 1, pending: 0, parts: [], id: "ap-phish", links: [], code: "T1566" },
    { label: "Valid Accounts", success: 0, failed: 0, pending: 0, parts: [], id: "ap-valid", links: [], code: "T1078" },
  ],
}; // prettier-ignore

/** An attack path: its simulation's techniques, linked, with the simulation's dates. */
const PATH: WidgetOutcomesData = {
  chains: [{ id: "path", label: "", groups: [{ id: "ta-init", label: "Initial Access" }, { id: "ta-exec", label: "Execution" }] }],
  items: [
    { label: "Phishing", success: 2, failed: 1, pending: 0, parts: [], id: "ap-phish", code: "T1566", phases: ["ta-init"], links: ["ap-cmd", "ap-valid"], link: `${BASE}/admin/simulations/sim-1?query=cGFnZT0w` },
    { label: "Valid Accounts", success: 0, failed: 1, pending: 0, parts: [], id: "ap-valid", code: "T1078", phases: ["ta-init", "ta-exec"], links: ["ap-cmd"] },
    { label: "Command and Scripting Interpreter", success: 1, failed: 0, pending: 0, parts: [], id: "ap-cmd", code: "T1059", phases: ["ta-exec"], links: [] },
  ],
  timeline: { start: "2026-10-20T09:00:00Z", end: "2026-10-20T11:15:30Z" },
}; // prettier-ignore

const EMPTY: WidgetOutcomesData = { items: [{ label: "Prevention", success: 0, failed: 0, pending: 0, parts: [] }] };

beforeAll(() => {
  // React Flow measures its container through ResizeObserver, which happy-dom lacks.
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

beforeEach(() => {
  // Reduced motion: every figure is drawn at once, without counting up.
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const settle = () => act(() => vi.advanceTimersByTime(100));

// ── Score gauge ──────────────────────────────────────────────────────────────

describe("the score gauge", () => {
  it("draws the failed share of resolved checks on the gauge, its band, and the first four labels' own", () => {
    draw(SCORE, "scoregauge");
    settle();
    // 77 failed out of 202 resolved: 38, moderate.
    expect(screen.getByRole("img", { name: "Exposure score 38 out of 100" })).toBeTruthy();
    expect(screen.getByText("38")).toBeTruthy();
    expect(screen.getByText("/ 100")).toBeTruthy();
    expect(screen.getByText("Moderate exposure").getAttribute("fill")).toBe("#ffa726");
    // Four buckets, in the order they came: 28, 46, 43, 0.
    expect(screen.getAllByText(/^(Prevention|Detection|Vulnerability|Custom probe|Manual)$/).map((node) => node.textContent)).toEqual(["Prevention", "Detection", "Vulnerability", "Custom probe"]);
    expect(["28", "46", "43", "0"].map((value) => (screen.getByText(value) as HTMLElement).style.color)).toEqual(["#ffa726", "#ffa726", "#ffa726", "#17ab1f"]);
  });

  it("shows its sample greyed while nothing is counted, or that there is no data", () => {
    draw(EMPTY, "scoregauge", { sampleDisplay: "full" });
    settle();
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(document.querySelector("[data-sample='full']")).not.toBeNull();
    expect(screen.getByRole("img", { name: "Exposure score 38 out of 100", hidden: true })).toBeTruthy();
    cleanup();
    draw(EMPTY, "scoregauge");
    expect(screen.getByText("No data for this period.")).toBeTruthy();
  });
});

// ── Ring gauge ───────────────────────────────────────────────────────────────

describe("the ring gauge", () => {
  it("draws the statuses on a ring, the resilience in the centre, a legend, and says what is clicked", () => {
    const onSelect = draw(GAUGE, "ringgauge");
    const ring = screen.getByRole("button", { name: "Detection: 70% resilience" });
    expect(within(ring).getByText("70%").getAttribute("fill")).toBe("#ffa726");
    expect([...ring.querySelectorAll("[data-status]")].map((arc) => [arc.getAttribute("data-status"), arc.getAttribute("stroke")])).toEqual([
      ["success", "#17ab1f"],
      ["failed", "#f14337"],
    ]);
    // No pending: no pending entry.
    expect(screen.queryByText("Pending")).toBeNull();
    expect(screen.getByText("64")).toBeTruthy();
    expect(screen.getByTestId("ring-gauge.glyph")).toBeTruthy();
    fireEvent.click(ring);
    fireEvent.click(screen.getByText("Failed"));
    expect(onSelect?.mock.calls).toEqual([[{ name: "Detection", value: "SUCCESS,FAILED,PENDING" }], [{ name: "Detection", value: "FAILED" }]]);
  });

  it("shows the sample statuses greyed while nothing is counted", () => {
    draw(EMPTY, "ringgauge", { sampleDisplay: "full" });
    expect(screen.getByText("Sample")).toBeTruthy();
    // 64 / 91 resolved.
    expect(screen.getByText("70%")).toBeTruthy();
    expect(screen.getByText("Pending")).toBeTruthy();
  });
});

// ── Domain cards ─────────────────────────────────────────────────────────────

describe("the domain cards", () => {
  it("lists every domain in order, measured or not, opens one at a time, and says the status clicked", () => {
    const onSelect = draw(DOMAINS, "domaincards");
    const cards = screen.getAllByRole("button", { expanded: false });
    expect(cards.map((card) => card.textContent)).toEqual(["Endpoint77%", "Cloud—"]);
    // 33 of 43 resolved: 77, the success band.
    expect((within(cards[0]).getByText("77%") as HTMLElement).style.color).toBe("rgb(2, 129, 8)");
    fireEvent.click(cards[0]);
    const panel = screen.getByTestId("domain-cards.panel");
    expect(within(panel).getAllByRole("button").map((tile) => tile.textContent)).toEqual(["71%", "24%", "5%", "100%"]);
    fireEvent.click(within(panel).getByText("24%"));
    expect(onSelect?.mock.calls).toEqual([[{ name: "Endpoint", x: "PREVENTION", value: "FAILED" }]]);
    // Another card replaces it; the open one closes on a second click.
    fireEvent.click(screen.getByRole("button", { name: /Cloud/ }));
    expect(screen.getByText("No data collected on this domain at this time. Run a scenario to start analyzing your position on this domain.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Cloud/ }));
    expect(screen.queryByTestId("domain-cards.panel")).toBeNull();
  });
});

// ── Technique matrix ─────────────────────────────────────────────────────────

describe("the technique matrix", () => {
  it("draws the opening chain's columns, its coverage and legend, and says the technique clicked with its sub-techniques", () => {
    const onSelect = draw(COVERAGE, "outcomematrix");
    expect(screen.getByRole("combobox", { name: "Kill chain" }).textContent).toContain("MITRE ATT&CK");
    // 2 of 3 techniques have outcomes: 67%, in the accent of a rate over a half (amber).
    expect((screen.getByText("67%") as HTMLElement).style.color).toBe("#ffb300");
    expect(screen.getByText("2/3 tested")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Covered 2" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Gaps 1" })).toBeTruthy();
    expect(within(screen.getByTestId("outcome.rate-legend")).getAllByText(/%/).map((label) => label.textContent)).toEqual(["≤ 100%", "≤ 75%", "≤ 50%", "≤ 25%"]);
    // A column per phase, each titled with the phase's own name as OpenAEV shows it; techniques by name.
    expect(screen.getAllByRole("region").filter((region) => region.tagName === "SECTION").map((column) => [column.getAttribute("aria-label"), within(column).getAllByTestId("outcome.box").map((box) => box.textContent)])).toEqual([
      ["INITIAL ACCESS", ["T15662/3Phishing", "T1078Valid Accounts"]],
      ["Execution", ["T1059Command and Scripting Interpreter", "T1078Valid Accounts"]],
    ]);
    const boxes = screen.getAllByTestId("outcome.box");
    expect(boxes.map((box) => box.getAttribute("data-tested"))).toEqual(["true", "false", "false", "false"]);
    expect((boxes[0].querySelector("[data-testid='outcome.meter']") as HTMLElement).style.width).toBe("67%");
    fireEvent.click(boxes[0]);
    expect(onSelect?.mock.calls).toEqual([[{ name: "Phishing", value: "ap-phish", row: { id: "ap-phish", code: "T1566", ids: "ap-phish,ap-phish-link" } }]]);
  });

  it("shows the covered techniques, the gaps, or that a scope is empty", () => {
    draw(COVERAGE, "outcomematrix");
    fireEvent.click(screen.getByRole("button", { name: "Gaps 1" }));
    expect(screen.getAllByTestId("outcome.box").map((box) => box.textContent)).toEqual(["T1059Command and Scripting Interpreter"]);
    fireEvent.click(screen.getByRole("button", { name: "Covered 2" }));
    expect(screen.getAllByTestId("outcome.box")).toHaveLength(3);
    cleanup();
    draw({ ...COVERAGE, items: [] }, "outcomematrix");
    fireEvent.click(screen.getByRole("button", { name: "Covered 0" }));
    expect(screen.getByText("No data to display")).toBeTruthy();
  });
});

// ── Path graph ───────────────────────────────────────────────────────────────

describe("the path graph", () => {
  it("lays out the phases and techniques coloured by their rate, the simulation's dates, the legend on the right", () => {
    const onSelect = draw(PATH, "pathgraph");
    const graph = screen.getByTestId("path-graph");
    expect(within(graph).getByText("Initial Access")).toBeTruthy();
    expect(within(graph).getByText("Execution")).toBeTruthy();
    // Valid Accounts sits under both phases; every node shows its rate and count (drawn as intended).
    const boxes = within(graph).getAllByTestId("outcome.box");
    expect(boxes.map((box) => [box.textContent, box.getAttribute("data-tested")])).toEqual([
      ["T15662/3Phishing", "true"],
      ["T10780/1Valid Accounts", "true"],
      ["T10780/1Valid Accounts", "true"],
      ["T10591/1Command and Scripting Interpreter", "true"],
    ]);
    // The start and the end, in the long date-time format.
    expect(within(graph).getAllByText(/^October 20, 2026 at \d/).map((node) => node.textContent)).toHaveLength(2);
    fireEvent.click(boxes[0]);
    expect(onSelect?.mock.calls).toEqual([[{ name: "Phishing", value: "ap-phish" }]]);
  });

  it("opens the simulation's injects on a technique when the dashboard sets no interaction", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    draw(PATH, "pathgraph", {}, null);
    fireEvent.click(within(screen.getByTestId("path-graph")).getAllByTestId("outcome.box")[0]);
    expect(open.mock.calls).toEqual([[`${BASE}/admin/simulations/sim-1?query=cGFnZT0w`, "_blank", "noopener,noreferrer"]]);
  });

  it("asks for a simulation when it has none", () => {
    draw({ items: [], needsScope: true }, "pathgraph");
    expect(screen.getByText("You must set a simulation")).toBeTruthy();
    expect(screen.queryByTestId("path-graph")).toBeNull();
  });
});

// ── Exposure console ─────────────────────────────────────────────────────────

describe("the exposure console", () => {
  it("draws the orb, the gates of the attack flow, the orbit and the calls to action", () => {
    const onSelect = draw(CENTER, "exposureconsole");
    settle();
    const orb = screen.getByTestId("exposure-console.orb");
    // 77 failed out of 204 resolved: 38, moderate.
    expect(within(orb).getByText("38")).toBeTruthy();
    expect(within(screen.getByTestId("exposure-console.chip")).getByText("Moderate exposure")).toBeTruthy();
    expect(screen.getByText("77 gaps to remediate")).toBeTruthy();
    // The platforms orbit by their type's abbreviation, a long name cut.
    const orbit = screen.getByTestId("exposure-console.orbit");
    expect([...orbit.querySelectorAll("[data-member]")].map((member) => member.getAttribute("data-member"))).toEqual(["EDR", "EMAIL_SECURITY", "CUSTOM"]);
    expect(within(orbit).getByText("ES")).toBeTruthy();
    expect(within(orbit).getByText("CrowdStrike Falcon In…")).toBeTruthy();
    // A gate per type with a check, its colour and its rate's.
    const gates = screen.getAllByTestId("exposure-console.gate");
    expect(gates.map((gate) => [gate.querySelector("text title")?.textContent, gate.getAttribute("data-color"), gate.getAttribute("data-rate-color")])).toEqual([
      ["Vulnerability", "#0fbcff", "#ffa726"],
      ["Prevention", "#0fbcff", "#ffa726"],
      ["Detection", "#0fbcff", "#ffa726"],
      ["Custom probe", "#22d3ee", "#17ab1f"],
    ]);
    expect(within(gates[1]).getByText("73%")).toBeTruthy();
    expect(within(gates[1]).getByText("73% prevented")).toBeTruthy();
    expect(within(screen.getByTestId("exposure-console.adversary")).getByText("213")).toBeTruthy();
    // The exact count is its title.
    expect(within(screen.getByTestId("exposure-console.breached")).getAllByText("77").map((node) => node.tagName.toLowerCase())).toEqual(["text", "title"]);
    // The calls to action link into the module.
    const actions = within(screen.getByTestId("exposure-console.actions")).getAllByRole("link");
    expect(actions.map((action) => [action.getAttribute("aria-label"), action.getAttribute("href")])).toEqual([
      ["Design a scenario", `${BASE}/admin/scenarios`],
      ["Review findings", `${BASE}/admin/findings`],
    ]);
    fireEvent.click(within(gates[1]).getByRole("button", { name: "Prevention" }));
    fireEvent.click(within(screen.getByTestId("exposure-console.breached")).getByRole("button", { name: "Breached" }));
    expect(onSelect?.mock.calls).toEqual([[{ name: "Prevention", value: "PREVENTION" }], [{ name: "breach", value: "breach" }]]);
  });

  it("explains its score: the formula, the pillars and the current band", () => {
    draw(CENTER, "exposureconsole");
    fireEvent.click(screen.getByTestId("exposure-console.chip"));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Adversarial exposure score")).toBeTruthy();
    expect(within(dialog).getByText("77 of 204 validations breached your controls.")).toBeTruthy();
    expect(within(dialog).getAllByTestId("exposure-console.pillar").map((pillar) => pillar.firstElementChild?.textContent)).toEqual([
      "Vulnerability17 / 40 breached (43%)",
      "Prevention22 / 80 breached (28%)",
      "Detection38 / 82 breached (46%)",
      "Custom probe0 / 2 breached (0%)",
    ]);
    expect(dialog.querySelector("[data-current='true']")?.textContent).toContain("25 - 49 - Moderate exposure - current");
  });

  it("shows its sample desaturated while nothing is counted", () => {
    draw({ ...EMPTY, orbit: CENTER.orbit }, "exposureconsole", { sampleDisplay: "subtle" });
    expect(document.querySelector("[data-sample='subtle']")).not.toBeNull();
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(screen.getAllByTestId("exposure-console.gate").map((gate) => gate.querySelector("text title")?.textContent)).toEqual(["Vulnerability", "Prevention", "Detection"]);
  });
});
