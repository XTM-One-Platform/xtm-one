// SPDX-License-Identifier: Apache-2.0
// CTEM-08-UI (#998): the loop trace names the trigger rule that started a
// signal ("vulnerability on software we run", or a PIR score held to the
// policy) as its own row, never as raw evidence.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { LoopTraceDialog, signalTriggerRule } from "./LoopTraceDialog";

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
  decision: null,
  case_id: null,
  recorded_via: "listener",
};

afterEach(() => get.mockReset());

describe("signal trigger rule", () => {
  it("reads the software first, then the highest PIR score, else nothing", () => {
    expect(signalTriggerRule({ software: ["nginx"] })).toEqual({ rule: "software", software: "nginx" });
    expect(signalTriggerRule({ pir: [{ pir_id: "a", score_at_delivery: 72 }, { pir_id: "b", score_at_delivery: 81 }] })).toEqual({ rule: "pir", score: 81 });
    expect(signalTriggerRule({ software: [""], pir: [] })).toBeUndefined();
    expect(signalTriggerRule({ event: "create" })).toBeUndefined();
    expect(signalTriggerRule(null)).toBeUndefined();
  });

  it("the trace names the software rule in its own row, not in the evidence", async () => {
    get.mockResolvedValue(onSoftware);
    renderWithProviders(
      <MemoryRouter initialEntries={[`/?loop=signal:${ID}`]}>
        <LoopTraceDialog />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    expect(within(facts).getByText("Trigger rule")).toBeTruthy();
    expect(within(facts).getByText("Vulnerability on software we run: nginx 1.27.1")).toBeTruthy();
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("relationship:has");
    expect(evidence).not.toHaveTextContent("software:");
  });

  it("a signal from another rule shows no trigger rule row", async () => {
    get.mockResolvedValue({ ...onSoftware, evidence: { event: "create" } });
    renderWithProviders(
      <MemoryRouter initialEntries={[`/?loop=signal:${ID}`]}>
        <LoopTraceDialog />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    expect(within(facts).queryByText("Trigger rule")).toBeNull();
  });
});
