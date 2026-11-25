// SPDX-License-Identifier: Apache-2.0
// #874 S2: the reconcile and scope gates are drawn as steps of the loop trace,
// each with its decision, its reason and, where the signal stopped, its next step.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { gateStep } from "./DecisionReason";
import { SignalTraceBody } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000874";
const t = (id: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id);

const base: CtemSignal = {
  id: ID,
  trigger_path: "new_vulnerability",
  trigger_path_label: "New vulnerability on a running version",
  lens: "world",
  source: { provider: "opencti", external_id: "relationship--1" },
  subject: { kind: "vulnerability", ref: "vulnerability--1", label: "CVE-2026-0874" },
  observed_at: "2026-11-21T08:00:00Z",
  first_seen_at: "2026-11-21T08:00:00Z",
  last_seen_at: "2026-11-21T08:00:00Z",
  seen_count: 1,
  evidence: {},
  state: "watchlist",
  decision: {
    version: 1,
    outcome: "watchlist",
    reason_code: "below_scope_threshold",
    reason_params: { criticality: "low", threshold: "high" },
    reconcile: { confidence: "high" },
    decided_at: "2026-11-21T08:06:00Z",
  },
  case_id: null,
  recorded_via: "listener",
};

async function renderGates(signal: CtemSignal) {
  get.mockResolvedValue(signal);
  renderWithProviders(
    <MemoryRouter>
      <SignalTraceBody id={ID} />
    </MemoryRouter>,
  );
  const stages = await screen.findByTestId("ctem-loop-stages");
  return within(stages).queryAllByTestId("ctem-loop-gate");
}

afterEach(() => get.mockReset());

describe("gateStep", () => {
  it("is null outside the two gates and on a gate not reached or still waiting", () => {
    expect(gateStep("signal", "passed", "new", null, t)).toBeNull();
    expect(gateStep("case", "passed", "case_opened", null, t)).toBeNull();
    expect(gateStep("reconcile", "current", "new", null, t)).toBeNull();
    expect(gateStep("scope", "not_reached", "new", null, t)).toBeNull();
  });

  it("a passed reconcile reads Confirmed with its confidence; a passed scope gate reads In scope", () => {
    expect(gateStep("reconcile", "passed", "watchlist", base.decision, t)).toEqual({ decision: "Confirmed", reason: "Confidence High", next: null });
    const inScope = { outcome: "case", reason_code: "in_scope", reason_params: { criticality: "high", threshold: "high", confidence: "medium" } };
    expect(gateStep("scope", "passed", "case_opened", inScope, t)).toEqual({
      decision: "In scope",
      reason: "In scope: criticality High (threshold High), confidence Medium",
      next: null,
    });
  });

  it("the gate a signal stopped at carries its state, reason and next step", () => {
    expect(gateStep("scope", "stopped", "watchlist", base.decision, t)).toEqual({
      decision: "Watchlist",
      reason: "Criticality Low below the scope threshold High",
      next: "Becomes a case if criticality rises to High",
    });
  });
});

describe("the loop trace draws both gates", () => {
  it("a watchlisted signal: reconcile confirmed, scope gate stopped with why and next step", async () => {
    const gates = await renderGates(base);
    expect(gates.map((g) => g.getAttribute("data-gate"))).toEqual(["reconcile", "scope"]);
    expect(gates[0]).toHaveTextContent("Confirmed");
    expect(gates[0]).toHaveTextContent("Confidence High");
    expect(gates[1]).toHaveTextContent("Watchlist");
    expect(gates[1]).toHaveTextContent("Criticality Low below the scope threshold High");
    expect(gates[1]).toHaveTextContent("Becomes a case if criticality rises to High");
  });

  it("a new signal draws no gate yet: reconcile waits", async () => {
    expect(await renderGates({ ...base, state: "new", decision: null })).toHaveLength(0);
  });
});
