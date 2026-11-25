// SPDX-License-Identifier: Apache-2.0
// CTEM-08-UI (#998): the loop trace says what the CTEM-08 trigger rules know
// about a signal in its own rows, never as raw evidence, and names the rule
// "vulnerability on software we run" only when the scope gate resolved the
// source to the software inventory (#1000 UX round 1 B2).

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, signalRuleFacts } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000080";

const onSoftware: CtemSignal = {
  id: ID,
  trigger_path: "new_vulnerability",
  trigger_path_label: "New vulnerability on a running version",
  lens: "world",
  source: { provider: "opencti", external_id: "relationship--1" },
  subject: { kind: "vulnerability", ref: "vulnerability--1", label: "CVE-2026-0001" },
  observed_at: "2026-11-21T08:00:00Z",
  first_seen_at: "2026-11-21T08:00:00Z",
  last_seen_at: "2026-11-21T08:00:00Z",
  seen_count: 1,
  evidence: { event: "create", stix_type: "relationship:has", software: ["nginx 1.27.1"] },
  state: "confirmed",
  decision: { version: 1, outcome: "in_scope", reconcile: { products: ["nginx"] } },
  case_id: null,
  recorded_via: "listener",
};

function renderTrace(signal: CtemSignal) {
  get.mockResolvedValue(signal);
  renderWithProviders(
    <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
      <SignalTraceBody id={ID} />
    </MemoryRouter>,
  );
  return screen.findByTestId("ctem-signal-facts");
}

afterEach(() => get.mockReset());

describe("signal rule facts", () => {
  it("matches the software rule only when the scope gate resolved the software", () => {
    expect(signalRuleFacts({ software: ["nginx"] }, { reconcile: { products: ["nginx"] } })).toEqual([
      { key: "software", software: "nginx", matched: true, checked: true },
    ]);
    expect(signalRuleFacts({ software: ["nginx"] }, { reconcile: { products: [] } })).toEqual([
      { key: "software", software: "nginx", matched: false, checked: true },
    ]);
    expect(signalRuleFacts({ software: ["nginx"] }, null)).toEqual([
      { key: "software", software: "nginx", matched: false, checked: false },
    ]);
  });

  it("reads the highest PIR score at delivery, keeps both facts, else nothing", () => {
    expect(signalRuleFacts({ pir: [{ pir_id: "a", score_at_delivery: 72 }, { pir_id: "b", score_at_delivery: 81 }] })).toEqual([
      { key: "pir", score: 81 },
    ]);
    expect(signalRuleFacts({ software: ["nginx"], pir: [{ pir_id: "a", score_at_delivery: 90 }] }).map((f) => f.key)).toEqual(["software", "pir"]);
    expect(signalRuleFacts({ software: [""], pir: [] })).toEqual([]);
    expect(signalRuleFacts({ event: "create" })).toEqual([]);
    expect(signalRuleFacts(null)).toEqual([]);
  });

  it("the trace names the software rule in its own row when the source is in the software", async () => {
    const facts = await renderTrace(onSoftware);
    expect(within(facts).getByText("Trigger rule")).toBeTruthy();
    expect(within(facts).getByText("Vulnerability on software we run: nginx 1.27.1")).toBeTruthy();
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("relationship:has");
    expect(evidence).not.toHaveTextContent("software:");
  });

  it("a software signal not resolved to the software claims no trigger rule", async () => {
    const facts = await renderTrace({ ...onSoftware, state: "discarded", decision: { version: 1, reconcile: { products: [] } } });
    expect(within(facts).queryByText("Trigger rule")).toBeNull();
    expect(within(facts).getByText("Source software")).toBeTruthy();
    expect(within(facts).getByText("nginx 1.27.1, not in your software inventory")).toBeTruthy();
  });

  it("a PIR flag is stated as a fact, and both facts show when the evidence carries both", async () => {
    const facts = await renderTrace({
      ...onSoftware,
      evidence: { event: "create", software: ["nginx 1.27.1"], pir: [{ pir_id: "a", score_at_delivery: 62 }, { pir_id: "b", score_at_delivery: 88 }] },
    });
    expect(within(facts).getByText("PIR match")).toBeTruthy();
    expect(within(facts).getByText("Flagged in a PIR (score at delivery 88)")).toBeTruthy();
    expect(within(facts).getByText("Vulnerability on software we run: nginx 1.27.1")).toBeTruthy();
    expect(within(facts).getByTestId("ctem-signal-evidence")).not.toHaveTextContent("pir:");
  });

  it("a signal from another rule shows no rule row", async () => {
    const facts = await renderTrace({ ...onSoftware, evidence: { event: "create" } });
    expect(within(facts).queryByText("Trigger rule")).toBeNull();
    expect(within(facts).queryByText("PIR match")).toBeNull();
  });
});
