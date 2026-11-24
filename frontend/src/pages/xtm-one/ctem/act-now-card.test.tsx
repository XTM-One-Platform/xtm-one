// The Act now card of the CTEM command center (C2 slice V2, refocused by
// #10721): five one-line rows, the first marked "Do first", the whole row
// opening its target, no button column; the work items and approvals waiting
// for the reader first; never a briefing step or a stage assessment.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Zap } from "lucide-react";

import { renderWithProviders } from "@/testing/providers";
import { ActNowCard } from "./ActNowCard";
import type { ActNowRow } from "./act-now";

const state = vi.hoisted(() => ({
  waiting: { count: 0, oldest: null as null | { id: string; title: string; reason: null; since: null } },
  approvals: { tools: [] as { tool_name: string; run_count: number; call_count: number }[], mixed: [], mixed_run_count: 0 },
  assessmentCalls: 0,
  isAdmin: true,
  version: null as number | null,
  navigated: [] as string[],
  cases: [] as { id: string; title: string; state: string; severity: string | null; priority?: string; sla_due_at: string | null }[],
  decisions: [] as { id: string; correlation_key: string; needs_human: boolean; status: string }[],
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCases: () => ({ data: { cases: state.cases } }),
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => (to: string) => state.navigated.push(to),
}));
vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: state.isAdmin }),
}));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { version: state.version } }),
}));

vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItemsWaitingForMe: () => ({ data: state.waiting }),
  useWorkItems: () => ({ data: state.decisions }),
}));
vi.mock("@/hooks/api/useAssignmentApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAssignmentApi")>()),
  usePendingApprovalsByTool: () => ({ data: state.approvals }),
}));
vi.mock("@/hooks/api/useCtemMetricsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemMetricsApi")>()),
  useCtemStageAssessments: () => {
    state.assessmentCalls += 1;
    return [];
  },
  useRefreshStageAssessment: () => {
    throw new Error("the Act now card never generates an assessment");
  },
}));
vi.mock("@/components/ui/enterprise-upsell", () => ({
  AiLockedFooter: ({ reason }: { reason: string | null }) => <p>AI locked: {String(reason)}</p>,
}));

function row(id: string, priority: number, over: Partial<ActNowRow> = {}): ActNowRow {
  return {
    id, priority, icon: Zap, tone: "", title: `Row ${id}`, reason: `Why ${id}`, cta: "Open",
    onAction: () => undefined, source: "module", ...over,
  };
}

function card(rows: ActNowRow[], briefingActions: string[] = [], aiLockReason: null | "ai_off" = null, onContentHeight?: (px: number) => void) {
  return renderWithProviders(
    <MemoryRouter>
      <ActNowCard rows={rows} briefingActions={briefingActions} aiLockReason={aiLockReason} onContentHeight={onContentHeight} />
    </MemoryRouter>,
  );
}

const items = () => within(screen.getByTestId("ctem-act-now-rows")).getAllByRole("listitem");
const sources = () => items().map((li) => li.getAttribute("data-source"));

beforeEach(() => {
  state.waiting = { count: 0, oldest: null };
  state.approvals = { tools: [], mixed: [], mixed_run_count: 0 };
  state.assessmentCalls = 0;
  state.isAdmin = true;
  state.version = null;
  state.navigated = [];
  state.cases = [];
  state.decisions = [];
  sessionStorage.clear();
});

describe("ActNowCard", () => {
  it("says on a case row that its remediation decision waits (CTEM-17-UI)", () => {
    state.cases = [
      { id: "c1", title: "Upgrade nginx to 1.27.4", state: "new", severity: "P1", sla_due_at: null },
      { id: "c2", title: "Patch a P2", state: "new", severity: "P2", sla_due_at: null },
    ];
    state.decisions = [
      { id: "w1", correlation_key: "ctem:remediation:c1", needs_human: true, status: "blocked" },
      { id: "w2", correlation_key: "ctem:remediation:c2", needs_human: false, status: "done" },
    ];
    card([]);
    const rows = items();
    expect(within(rows[0]).getByText(/Remediation decision waits/)).toBeTruthy();
    expect(within(rows[1]).queryByText(/Remediation decision waits/)).toBeNull();
  });

  it("puts the work items and approvals waiting for the reader first, fleets off", () => {
    state.waiting = { count: 2, oldest: { id: "w1", title: "Approve the patch window", reason: null, since: null } };
    state.approvals = { tools: [{ tool_name: "send_email", run_count: 3, call_count: 3 }], mixed: [], mixed_run_count: 1 };
    card([row("kev", 95)]);
    expect(sources()).toEqual(["work_item", "approval", "module"]);
    expect(screen.getByText("2 work items wait for you")).toBeTruthy();
    expect(screen.getByText("4 runs wait for your approval")).toBeTruthy();
  });

  it("puts an open P1 case of the loop first, as Do first, with its SLA (#892)", () => {
    state.waiting = { count: 1, oldest: { id: "w1", title: "Approve the patch window", reason: null, since: null } };
    state.cases = [
      { id: "c1", title: "Close the exposure of edge-01", state: "new", severity: "P1", sla_due_at: new Date(Date.now() + 5 * 3_600_000).toISOString() },
      { id: "c2", title: "Patch a P2", state: "new", severity: "P2", sla_due_at: null },
    ];
    card([row("kev", 95)]);
    // Since CTEM-30 (#937) a P2 case is listed too, below what waits on the reader.
    expect(sources()).toEqual(["case", "work_item", "case"]);
    expect(within(items()[0]).getByText("Do first")).toBeTruthy();
    expect(within(items()[0]).getByText("Close the exposure of edge-01")).toBeTruthy();
    expect(within(items()[2]).getByText("Patch a P2")).toBeTruthy();
    expect(within(items()[2]).queryByText("Do first")).toBeNull();
  });

  it("shows a case row's own urgency, the P1 chip of the case, never a High band (UX round 2 on #897, B3)", () => {
    state.cases = [
      { id: "c1", title: "Close the exposure of edge-01", state: "new", severity: "P1", priority: "critical", sla_due_at: new Date(Date.now() + 5 * 3_600_000).toISOString() },
    ];
    card([row("kev", 95)]);
    const first = items()[0];
    expect(within(first).getByTestId("ctem-act-now-case-urgency").textContent).toContain("P1");
    expect(within(first).queryByText("High")).toBeNull();
  });

  it("marks the first row Do first, and only the first", () => {
    card([row("a", 95), row("b", 80), row("c", 70)]);
    const first = within(items()[0]).getByText("Do first");
    expect(first).toBeTruthy();
    expect(screen.getAllByText("Do first")).toHaveLength(1);
  });

  it("shows a cut title in full in a tooltip on the row button, on keyboard focus as on hover", async () => {
    // #10723 UX r1 B1: the tooltip sat on a span inside the button, which
    // never takes focus. jsdom lays nothing out, so every box reads as cut.
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(935);
    const client = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(629);
    try {
      const title = "Enable block mode for ransomware behavioural protections on EDR and re-run the simulation";
      card([row("long", 95, { title, cta: "Open" })]);
      const button = screen.getByRole("button", { name: `Open: ${title}` });
      act(() => button.focus());
      expect(await screen.findByRole("tooltip")).toHaveTextContent(title);
      act(() => button.blur());
      fireEvent.pointerMove(button, { pointerType: "mouse" });
      expect(await screen.findByRole("tooltip", {}, { timeout: 2000 })).toHaveTextContent(title);
    } finally {
      scroll.mockRestore();
      client.mockRestore();
    }
  });

  it("keeps the tooltip when a title goes from fitting to cut and the row is measured again", async () => {
    // #10723 UX r2 B1: wrapping the button in its tooltip mounts the title span
    // again; the observer stayed on the detached span, which measures 0, and
    // the next resize read the title as fitting. A detached node measures 0 here
    // as in a browser, and the observers fire on demand.
    let width = 600;
    const live = new Set<ResizeObserverCallback>();
    const original = globalThis.ResizeObserver;
    globalThis.ResizeObserver = class {
      private readonly cb: ResizeObserverCallback;
      constructor(cb: ResizeObserverCallback) { this.cb = cb; }
      observe() { live.add(this.cb); }
      unobserve() { live.delete(this.cb); }
      disconnect() { live.delete(this.cb); }
    } as unknown as typeof ResizeObserver;
    const resize = () => act(() => [...live].forEach((cb) => cb([], {} as ResizeObserver)));
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.isConnected ? 935 : 0;
    });
    const client = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.isConnected ? width : 0;
    });
    try {
      width = 1000;
      const title = "Enable block mode for ransomware behavioural protections on EDR and re-run the simulation";
      card([row("long", 95, { title, cta: "Open" })]);
      width = 629;
      resize();
      resize();
      const button = screen.getByRole("button", { name: `Open: ${title}` });
      act(() => button.focus());
      expect(await screen.findByRole("tooltip")).toHaveTextContent(title);
    } finally {
      scroll.mockRestore();
      client.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("opens the row's target from the whole row, with no separate button per row", () => {
    const onAction = vi.fn();
    card([row("kev", 95, { title: "Triage 4 known-exploited exposures", cta: "Triage", onAction })]);
    // The row button opens the target; Ask AI is its sibling, never nested.
    const buttons = within(items()[0]).getAllByRole("button");
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual([
      "Triage: Triage 4 known-exploited exposures", "Ask AI: Triage 4 known-exploited exposures",
    ]);
    expect(buttons[0].querySelector("button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Triage: Triage 4 known-exploited exposures" }));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it("shows three rows by default, then the whole queue on Show all, and back (#11547 S2d)", () => {
    card(Array.from({ length: 8 }, (_, i) => row(`r${i}`, 90 - i, { source: "setup" })));
    expect(items()).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Show all (8)" }));
    expect(items()).toHaveLength(8);
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(items()).toHaveLength(3);
  });

  it("draws what waits by source as a bar, a count and a label per source, each row led by its source dot (#11547 S2d)", () => {
    state.waiting = { count: 1, oldest: null };
    card([
      row("kev", 95),
      row("det", 90, { stage: "discovery" }),
      row("connect-validation", 100, { source: "setup" }),
      row("wire", 50, { source: "setup" }),
    ]);
    expect(screen.getByRole("img", { name: "What waits, by source: Posture 2, Blind spots 1, Setup 1, Waiting for you 1" })).toBeTruthy();
    const legend = within(screen.getByTestId("ctem-act-now-legend")).getAllByRole("listitem", { hidden: true }).map((li) => li.textContent);
    expect(legend).toEqual(["2Posture", "1Blind spots", "1Setup", "1Waiting for you"]);
    expect(within(items()[0]).getByTestId("ctem-act-now-dot").getAttribute("data-group")).toBe("blind_spots");
  });

  it("draws no bar when nothing waits", () => {
    card([], [], "ai_off");
    expect(screen.queryByTestId("ctem-act-now-bar")).toBeNull();
  });

  it("repeats neither the briefing's next steps nor the stage assessments, and reads no assessment", () => {
    card(
      [row("step", 92, { source: "briefing", title: "Patch the VPN" }), row("dup", 85, { title: "Patch the VPN" }), row("kev", 95)],
      ["Patch the VPN"],
    );
    expect(sources()).toEqual(["module"]);
    expect(screen.queryByText("Patch the VPN")).toBeNull();
    expect(state.assessmentCalls).toBe(0);
  });

  it("draws the failed expectations of one stage as one row", () => {
    card([
      row("det", 90, { stage: "validation", title: "Detection: 232 failed expectations" }),
      row("prev", 88, { stage: "validation", title: "Prevention: 232 failed expectations" }),
    ]);
    expect(items()).toHaveLength(1);
  });

  it("reports the height of its content so the page sizes the card to its rows", () => {
    const heights: number[] = [];
    card([row("kev", 95)], [], null, (px) => heights.push(px));
    expect(heights.length).toBeGreaterThan(0);
    expect(heights.every((px) => Number.isFinite(px) && px > 0)).toBe(true);
  });

  it("shows each row's why and no source label (#11213)", () => {
    card([row("kev", 95, { reason: "Actively exploited in the wild" })]);
    expect(within(items()[0]).getByTestId("ctem-act-now-why")).toHaveTextContent("Actively exploited in the wild");
    expect(screen.queryByText("Module")).toBeNull();
  });

  it("has no All work link: no page lists these rows", () => {
    card([row("kev", 95)]);
    expect(screen.queryByRole("button", { name: /All work/ })).toBeNull();
  });

  it("says the module facts are generic, with the setup call to action for an administrator", () => {
    card([row("kev", 95)]);
    expect(screen.getByText("These actions are generic")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up organization context" }).getAttribute("href")).toBe("/settings/organization?setup=1");
  });

  it("offers a review of the context when a profile exists, and only words it for anyone else", () => {
    state.version = 1;
    const { unmount } = card([row("kev", 95)]);
    expect(screen.getByRole("link", { name: "Review organization context" })).toBeTruthy();
    unmount();
    state.isAdmin = false;
    card([row("kev", 95)]);
    expect(screen.getByTestId("ctem-act-now-generic")).toHaveTextContent("An administrator can set it up");
    expect(screen.queryByRole("link", { name: /organization context/ })).toBeNull();
  });

  it("shows no generic notice when a fact is tailored, or when only the reader's own work waits", () => {
    const { unmount } = card([row("kev", 95, { tailored: true })]);
    expect(screen.queryByText("These actions are generic")).toBeNull();
    unmount();
    state.waiting = { count: 1, oldest: null };
    card([]);
    expect(screen.queryByText("These actions are generic")).toBeNull();
  });

  it("opens the chat with the row's context in the composer on Ask AI, never sent", () => {
    card([row("kev", 95, { title: "Triage 4 known-exploited exposures", reason: "Actively exploited", stage: "prioritization" })]);
    fireEvent.click(screen.getByRole("button", { name: "Ask AI: Triage 4 known-exploited exposures" }));
    expect(state.navigated).toEqual(["/chat"]);
    const draft = Object.keys(sessionStorage).map((k) => sessionStorage.getItem(k) ?? "").find((v) => v.includes("Triage 4"));
    expect(draft).toContain("guide me step by step");
    expect(draft).toContain("This action is generic");
    expect(Object.values(sessionStorage).some((v) => v === "1")).toBe(false);
  });

  it("offers no Ask AI on what waits for the reader, nor when AI is locked", () => {
    state.waiting = { count: 1, oldest: null };
    const { unmount } = card([]);
    expect(within(items()[0]).getAllByRole("button")).toHaveLength(1);
    unmount();
    state.waiting = { count: 0, oldest: null };
    card([row("kev", 95)], [], "ai_off");
    expect(screen.queryByRole("button", { name: /^Ask AI/ })).toBeNull();
  });

  it("keeps one Ask AI column so the priority tags line up on mixed rows", () => {
    state.waiting = { count: 1, oldest: null };
    card([row("kev", 95, { title: "Triage 4 known-exploited exposures" })]);
    const rows = items();
    expect(rows).toHaveLength(2);
    // Every row ends with an Ask AI slot: the real button, or its hidden twin.
    const slots = rows.map((li) => within(li).queryByTestId("ctem-act-now-ask-ai-slot"));
    const real = rows.map((li) => within(li).queryByRole("button", { name: /^Ask AI/ }));
    rows.forEach((_, i) => expect(Boolean(slots[i]) !== Boolean(real[i])).toBe(true));
    // The twin is never reachable: hidden from assistive tech, out of the tab order.
    const twin = slots.find(Boolean)!;
    expect(twin.getAttribute("aria-hidden")).toBe("true");
    expect(twin.querySelector("button")?.getAttribute("tabindex")).toBe("-1");
  });

  it("explains itself instead of an empty body", () => {
    card([], [], "ai_off");
    expect(screen.getByText("No recommended action yet")).toBeTruthy();
  });
});
