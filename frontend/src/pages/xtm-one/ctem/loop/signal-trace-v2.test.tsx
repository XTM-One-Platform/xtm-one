// SPDX-License-Identifier: Apache-2.0
// #1979: the loop trace v2 keeps every fact of v1. The trace is read in sections
// (initial event, loop progress, policy funnel, scope and criticality, primary
// assets reached); this test renders a fully populated case-opened signal and
// checks that every label and value v1 drew is still on screen, the policy
// checks as funnel steps with their value against the threshold, the primary
// assets as cards with their real supporting asset total, and every evidence
// key (v1 kept 8).
// The per-family facts (Aikido, identity, evidence health, surface, freshness)
// keep their own tests (signal-*.test.tsx), which read the same `ctem-signal-facts`.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { decisionInputs } from "./DecisionReason";
import { SignalTraceBody } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000001979";
const t = (id: string, values?: Record<string, string | number>) =>
  id.replace(/\{(\w+)\}/g, (_, k: string) => String(values?.[k] ?? `{${k}}`));

const chain = (asset: string) => ({
  primary_asset_key: "employee workstations",
  software: "Windows",
  asset,
  through: "All Windows laptops",
  through_kind: "asset_group",
  source: "rule",
  roles: ["stores", "processes"],
  via: [],
});

const extraEvidence = Object.fromEntries(Array.from({ length: 10 }, (_, n) => [`extra_key_${n}`, `extra value ${n}`]));

const signal: CtemSignal = {
  id: ID,
  trigger_path: "new_vulnerability",
  trigger_path_label: "New vulnerability on a running version",
  lens: "world",
  source: { provider: "opencti", external_id: "vulnerability--1" },
  subject: { kind: "vulnerability", ref: "CVE-2026-69459", label: "CVE-2026-69459" },
  observed_at: "2026-11-16T15:47:00Z",
  first_seen_at: "2026-11-16T15:47:00Z",
  last_seen_at: "2026-11-16T15:47:00Z",
  seen_count: 72,
  evidence: { software: ["Microsoft Windows"], cvss: 7.8, epss: 0.42, version_note: "fixed version unknown", ...extraEvidence },
  state: "case_opened",
  decision: {
    version: 1,
    outcome: "case",
    reason_code: "in_scope",
    reason_params: { criticality: "high", threshold: "high", confidence: "low" },
    reconcile: {
      assets: [],
      lenses: ["world"],
      confidence: "low",
      primary_assets_named: ["Employee workstations"],
      products: ["Windows"],
      inventory_links_read: true,
      primary_assets: [chain("LAPTOP-00001"), chain("LAPTOP-00002"), chain("LAPTOP-00003"), chain("LAPTOP-00004"), chain("LAPTOP-00005"), chain("LAPTOP-00006")],
      primary_asset_totals: { "employee workstations": { assets: 10000, direct: 12, via_group: 9988 } },
    },
    scope: {
      criticality: "high",
      threshold: "high",
      risk_scenarios: [{ id: "scenario-1", name: "Ransomware on workstations" }],
      sources: [
        {
          from: "primary_asset",
          name: "Employee workstations",
          criticality: "high",
          chain: [chain("LAPTOP-00001"), chain("LAPTOP-00002"), chain("LAPTOP-00003"), chain("LAPTOP-00004"), chain("LAPTOP-00005")],
          chain_total: 6,
          assets_total: 10000,
        },
      ],
    },
    policy_checks: [
      { rule: "cvss_min", value: 7.8, threshold: 7, passed: true },
      { rule: "epss_min", value: 0.42, threshold: 0.1, passed: true },
      { rule: "min_primary_asset_criticality", value: "high", threshold: "high", passed: true },
    ],
  } as CtemSignal["decision"],
  case_id: "case-7",
  recorded_via: "listener",
};

function renderTrace(value: CtemSignal) {
  get.mockImplementation(async (url: string) => {
    if (url.includes("/supporting-assets")) {
      return {
        primary_asset: "employee workstations",
        total: 10000,
        offset: 0,
        limit: 10,
        truncated: false,
        assets: Array.from({ length: 10 }, (_, n) => ({
          asset: `LAPTOP-${String(n).padStart(5, "0")}`,
          software: "Windows",
          through: "All Windows laptops",
          through_kind: "asset_group",
          source: "rule",
          roles: ["stores"],
        })),
      };
    }
    return value;
  });
  renderWithProviders(
    <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
      <SignalTraceBody id={ID} />
    </MemoryRouter>,
  );
  return screen.findByTestId("ctem-signal-facts");
}

afterEach(() => get.mockReset());

describe("loop trace v2 (#1979)", () => {
  it("draws the five sections in order, the initial event first", async () => {
    await renderTrace(signal);
    const ids = ["initial-event", "progress", "funnel", "scope", "primary-assets"].map((id) => screen.getByTestId(`ctem-loop-trace-${id}`));
    for (let i = 1; i < ids.length; i += 1) {
      expect(ids[i - 1].compareDocumentPosition(ids[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    const hero = screen.getByTestId("ctem-loop-trace-initial-event");
    expect(within(hero).getByText("New vulnerability on a running version")).toBeTruthy();
    expect(within(hero).getByTestId("ctem-loop-trace-subject")).toHaveTextContent("CVE-2026-69459");
    expect(within(hero).getByTestId("ctem-decision-reason")).toBeTruthy();
    expect(within(hero).getByTestId("ctem-loop-trace-figure-cvss")).toHaveTextContent("7.8");
    expect(within(hero).getByTestId("ctem-loop-trace-figure-epss")).toHaveTextContent("0.42");
    expect(within(hero).getByTestId("ctem-loop-trace-figure-seen")).toHaveTextContent("72 times");
  });

  it("keeps every fact v1 drew: the origin rows, every decision input, every evidence key", async () => {
    const facts = await renderTrace(signal);
    for (const label of ["Subject", "Source", "Trigger rule", "First seen", "Last seen", "Seen"]) {
      expect(within(facts).getByText(label)).toBeTruthy();
    }
    expect(within(facts).getByText("Vulnerability on software we run: Microsoft Windows")).toBeTruthy();
    // Every input v1 listed is still there; the checks are the funnel and the chains the cards.
    const trace = screen.getByTestId("ctem-loop-trace-initial-event").parentElement as HTMLElement;
    for (const input of decisionInputs(signal.decision, t)) {
      if (input.key === "policy_checks" || input.key === "primary_assets_reached") continue;
      expect(within(trace).getAllByText(input.label).length, input.label).toBeGreaterThan(0);
    }
    // v1 cut the evidence at 8 keys: v2 draws them all.
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    for (let n = 0; n < 10; n += 1) expect(evidence).toHaveTextContent(`extra value ${n}`);
    expect(evidence).toHaveTextContent("fixed version unknown");
  });

  it("draws one funnel step per policy check with its value against the threshold, ending in the outcome", async () => {
    await renderTrace(signal);
    const funnel = screen.getByTestId("ctem-policy-funnel");
    const steps = within(funnel).getAllByRole("listitem");
    expect(steps.map((s) => s.getAttribute("data-rule"))).toEqual(["cvss_min", "epss_min", "min_primary_asset_criticality", "outcome"]);
    expect(steps[0]).toHaveTextContent("CVSS minimum");
    expect(steps[0]).toHaveTextContent("7.8 ≥ 7");
    expect(steps[0]).toHaveTextContent("Passed");
    expect(steps[0]).not.toHaveTextContent("cvss_min");
    expect(within(funnel).getByTestId("ctem-policy-funnel-outcome")).toHaveTextContent("Case opened");
    expect(within(funnel).getByTestId("ctem-policy-funnel-outcome")).toHaveTextContent("Scope threshold: High");
  });

  it("marks a failed check and stops the funnel there", async () => {
    await renderTrace({
      ...signal,
      state: "watchlist",
      decision: {
        ...signal.decision,
        outcome: "watchlist",
        reason_code: "score_below_policy",
        reason_params: { score: "CVSS", value: 5.3, minimum: 7 },
        policy_checks: [{ rule: "cvss_min", value: 5.3, threshold: 7, passed: false }],
      },
    });
    const step = within(screen.getByTestId("ctem-policy-funnel")).getAllByRole("listitem")[0];
    expect(step.getAttribute("data-passed")).toBe("false");
    expect(step).toHaveTextContent("5.3 < 7");
    expect(step).toHaveTextContent("Failed");
  });

  it("shows the real supporting asset total of a primary asset and lists them in a searchable table", async () => {
    await renderTrace(signal);
    const card = screen.getByTestId("ctem-primary-asset-card");
    expect(card).toHaveTextContent("Employee workstations");
    // Compact on the card (10K), exact on hover: the real total, never the 5 chains kept.
    expect(within(card).getByTestId("ctem-primary-asset-total")).toHaveTextContent("10K supporting assets");
    expect(within(card).getByTitle("10,000")).toBeTruthy();
    expect(card).toHaveTextContent("12 direct links");
    // UX r1 B2 on #1995: the via-group count is compact too, exact on hover.
    expect(within(card).getByTestId("ctem-primary-asset-via-group")).toHaveTextContent("10K through asset groups");
    expect(within(card).getByTitle("9,988")).toBeTruthy();
    // Five chains kept, the sixth counted, never silently cut.
    expect(within(within(card).getByTestId("ctem-primary-asset-chains")).getAllByRole("listitem")).toHaveLength(6);
    expect(card).toHaveTextContent("+1 more");
    fireEvent.click(within(card).getByTestId("ctem-primary-asset-toggle"));
    const table = await within(card).findByTestId("ctem-primary-asset-supporting");
    expect(await within(table).findByText("LAPTOP-00000")).toBeTruthy();
    expect(get).toHaveBeenCalledWith(expect.stringContaining(`/ctem-signals/${ID}/primary-assets/employee%20workstations/supporting-assets?offset=0&limit=10`));
  });

  it("sends the sort of the supporting table to the server (UX r1 B3 on #1995)", async () => {
    await renderTrace(signal);
    const card = screen.getByTestId("ctem-primary-asset-card");
    fireEvent.click(within(card).getByTestId("ctem-primary-asset-toggle"));
    const table = await within(card).findByTestId("ctem-primary-asset-supporting");
    await within(table).findByText("LAPTOP-00000");
    fireEvent.click(within(table).getByRole("button", { name: /^Asset/ }));
    await vi.waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining("sort=asset&direction=desc")));
    fireEvent.click(within(table).getByRole("button", { name: /^Through/ }));
    await vi.waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringMatching(/sort=through&direction=(asc|desc)/)));
  });

  it("draws the criticality tile as the level, the threshold on its own line (UX r1 B1 on #1995)", async () => {
    await renderTrace({ ...signal, decision: { ...signal.decision, scope: { ...signal.decision!.scope!, criticality: "critical" } } as CtemSignal["decision"] });
    const tile = screen.getByTestId("ctem-loop-trace-figure-criticality");
    expect(tile).toHaveTextContent("Critical");
    expect(tile).toHaveTextContent("≥ High");
    expect(tile).not.toHaveTextContent("at or above");
  });

  it("summarises a long matched asset list: the count, three names, the rest counted (UX r1 B5 on #1995)", async () => {
    const names = Array.from({ length: 2500 }, (_, n) => `ws-${String(n).padStart(5, "0")}`);
    const reconcile = { ...signal.decision!.reconcile!, assets: names, primary_asset_totals: { "employee workstations": { assets: 2500, direct: 0, via_group: 2500 } } };
    await renderTrace({ ...signal, decision: { ...signal.decision, reconcile } as CtemSignal["decision"] });
    const cell = screen.getByTestId("ctem-decision-input-matched_assets");
    expect(cell).toHaveTextContent("2.5K assets: ws-00000, ws-00001, ws-00002, +2.5K more");
    expect(cell.querySelector("[title]")).toBeNull();
  });

  it("never counts a capped matched list as the total, and names each asset once (UX r2 B7 on #1995)", async () => {
    const names = Array.from({ length: 4991 }, (_, n) => (n === 1 ? "vpn-gw-01" : n === 0 ? "vpn-gw-01" : `ws-${String(n).padStart(5, "0")}`));
    await renderTrace({
      ...signal,
      decision: { ...signal.decision, reconcile: { ...signal.decision!.reconcile!, assets: names, assets_capped: true } } as CtemSignal["decision"],
    });
    const cell = screen.getByTestId("ctem-decision-input-matched_assets");
    expect(cell).toHaveTextContent("At least 5K assets: vpn-gw-01, ws-00002, ws-00003, +5K more");
    expect(cell.textContent?.match(/vpn-gw-01/g)).toHaveLength(1);
  });

  // UX r2 B1 on #2533: the tooltip drew a joined string cut at 280 characters, so the last names were lost.
  it("draws every software name tried in the tooltip, the last of 14 long names included (UX r2 B1 on #2533)", async () => {
    const names = Array.from({ length: 14 }, (_, n) => `Vendor ${n} Enterprise Remote Access Gateway Appliance ${n}`);
    await renderTrace({
      ...signal,
      state: "to_validate",
      decision: { ...signal.decision, outcome: "to_validate", reason_code: "subject_unresolved", reconcile: { assets: [], software_refs: names } } as CtemSignal["decision"],
    });
    const cell = screen.getByTestId("ctem-decision-input-software_tried");
    fireEvent.focus(within(cell).getByText(/^14 /));
    const tip = (await screen.findAllByTestId("ctem-decision-input-tip-software_tried"))[0];
    expect(within(tip).getAllByRole("listitem")).toHaveLength(14);
    expect(tip).toHaveTextContent(names[13]);
  });

  it("draws the first 20 matched assets in the tooltip and counts the rest (UX r2 B1 on #2533)", async () => {
    const names = Array.from({ length: 40 }, (_, n) => `ws-${String(n).padStart(5, "0")}`);
    await renderTrace({ ...signal, decision: { ...signal.decision, reconcile: { ...signal.decision!.reconcile!, assets: names } } as CtemSignal["decision"] });
    fireEvent.focus(within(screen.getByTestId("ctem-decision-input-matched_assets")).getByText(/assets: /));
    const tip = (await screen.findAllByTestId("ctem-decision-input-tip-matched_assets"))[0];
    expect(within(tip).getAllByRole("listitem")).toHaveLength(21);
    expect(tip).toHaveTextContent("ws-00019");
    expect(tip).toHaveTextContent("+20 more");
  });

  it("reads a record older than the cap flag as capped when a primary asset total is larger (UX r2 B7 on #1995)", async () => {
    const names = Array.from({ length: 40 }, (_, n) => `ws-${String(n).padStart(5, "0")}`);
    await renderTrace({ ...signal, decision: { ...signal.decision, reconcile: { ...signal.decision!.reconcile!, assets: names } } as CtemSignal["decision"] });
    expect(screen.getByTestId("ctem-decision-input-matched_assets")).toHaveTextContent(/^At least 40 assets: /);
  });

  it("keeps the next step, the stages and Open case", async () => {
    await renderTrace(signal);
    const stages = screen.getByTestId("ctem-loop-stages");
    expect(within(stages).getAllByRole("listitem")).toHaveLength(10);
    expect(within(stages).getByTestId("ctem-loop-trace-open-case")).toHaveAttribute("href", "/ctem/cases/case-7");
  });

  it("still renders a record older than #1979: chains, no total", async () => {
    const legacy = structuredClone(signal);
    const decision = legacy.decision as { scope: { sources: Record<string, unknown>[] }; reconcile: Record<string, unknown> };
    delete decision.scope.sources[0].chain_total;
    delete decision.scope.sources[0].assets_total;
    delete decision.reconcile.primary_asset_totals;
    await renderTrace(legacy);
    const card = screen.getByTestId("ctem-primary-asset-card");
    expect(within(card).queryByTestId("ctem-primary-asset-total")).toBeNull();
    expect(card).toHaveTextContent("LAPTOP-00001");
  });
});
