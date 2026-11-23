// SPDX-License-Identifier: Apache-2.0
// CTEM-29-UI (#1109): a credential-leak or a third-party signal reads like any
// other in the loop trace: the reason codes the scope gate writes for them in
// words with their next step, the owned domain that put a leak in scope, and
// the facts of the delivery in named rows, never raw evidence lines and never
// a secret.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { decisionSentence, nextStepSentence } from "./DecisionReason";
import { SignalTraceBody, subjectFacts } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000290";
const t = (id: string, values?: Record<string, string | number>) =>
  id.replace(/\{(\w+)\}/g, (_, k: string) => String(values?.[k] ?? `{${k}}`));

const leak: CtemSignal = {
  id: ID,
  trigger_path: "credential_leak",
  trigger_path_label: "Credential leak",
  lens: "world",
  source: { provider: "opencti", external_id: "user-account--1" },
  subject: { kind: "identity", ref: "jdoe@corp.example", label: "jdoe@corp.example" },
  observed_at: "2026-11-22T06:00:00Z",
  first_seen_at: "2026-11-22T06:00:00Z",
  last_seen_at: "2026-11-22T06:00:00Z",
  seen_count: 1,
  evidence: {
    event: "create",
    stix_type: "user-account",
    account_disabled: false,
    password_fingerprint: "sha256:abc",
  },
  state: "case_opened",
  decision: {
    version: 1,
    outcome: "case",
    reason_code: "in_scope",
    reason_params: { criticality: "high", threshold: "high", confidence: "medium" },
    reconcile: { assets: [], crown_jewels_named: [], tech_stack: [] },
    scope: { criticality: "high", threshold: "high", sources: [{ from: "owned_domain", name: "corp.example", criticality: "high" }] },
  },
  case_id: null,
  recorded_via: "listener",
};

const supplier: CtemSignal = {
  ...leak,
  trigger_path: "third_party_event",
  trigger_path_label: "Third-party event",
  source: { provider: "opencti", external_id: "report--1:org" },
  subject: { kind: "third_party", ref: "Acme Hosting", label: "Acme Hosting" },
  evidence: { event: "create", stix_type: "report", report: "Acme Hosting breach", relationship: "provider", organization_criticality: "critical" },
  state: "watchlist",
  decision: { version: 1, outcome: "watchlist", reason_code: "no_dependency", reason_params: {}, reconcile: { assets: [] }, scope: null },
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

describe("credential-leak and third-party signals", () => {
  it("says the two CTEM-29 reason codes and what would make each a case", () => {
    const watched = { outcome: "watchlist", reason_code: "no_dependency", reason_params: {} };
    const disabled = { outcome: "discarded", reason_code: "account_disabled", reason_params: {} };
    expect(decisionSentence(watched, t)).toBe("No crown jewel depends on this organization");
    expect(nextStepSentence(watched, t)).toBe("Becomes a case once a crown jewel depends on this organization");
    expect(decisionSentence(disabled, t)).toBe("Not exposed: the account is disabled");
    expect(nextStepSentence(disabled, t)).toBe("Becomes a case if the account is enabled again");
  });

  it("reads the facts of a leak and of a third party only", () => {
    expect(subjectFacts("identity", leak.evidence)).toEqual([
      { key: "observed_as", type: "user-account" },
      { key: "account", disabled: false },
      { key: "fingerprint" },
    ]);
    expect(subjectFacts("third_party", supplier.evidence)).toEqual([
      { key: "named_in", type: "report", name: "Acme Hosting breach" },
      { key: "relationship", relationship: "provider" },
      { key: "organization_criticality", criticality: "critical" },
    ]);
    expect(subjectFacts("vulnerability", leak.evidence)).toEqual([]);
    expect(subjectFacts("identity", null)).toEqual([]);
  });

  it("the trace of a leak names the owned domain and never shows the fingerprint", async () => {
    const facts = await renderTrace(leak);
    expect(within(facts).getByText("User account")).toBeTruthy();
    expect(within(facts).getByText("Enabled")).toBeTruthy();
    expect(within(facts).getByText("Kept as a fingerprint only, never the value")).toBeTruthy();
    expect(screen.getByTestId("ctem-signal-facts")).not.toHaveTextContent("sha256:abc");
    expect(screen.getByTestId("ctem-signal-facts")).not.toHaveTextContent("stix_type");
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("Owned domain");
    expect(inputs).toHaveTextContent("corp.example");
    expect(inputs).not.toHaveTextContent("None matched");
  });

  it("the trace of a third party says why it is watched, in named rows", async () => {
    const facts = await renderTrace(supplier);
    expect(within(facts).getByText("Report: Acme Hosting breach")).toBeTruthy();
    expect(within(facts).getByText("Provider")).toBeTruthy();
    expect(within(facts).getByText("Critical")).toBeTruthy();
    expect(within(facts).queryByTestId("ctem-signal-evidence")?.textContent ?? "").not.toContain("relationship");
    expect(screen.getByTestId("ctem-decision-next-step")).toHaveTextContent("Becomes a case once a crown jewel depends on this organization");
  });

  it("the trace of a third party that opened a case says its crown jewels once, in Decision inputs", async () => {
    const dependent: CtemSignal = {
      ...supplier,
      evidence: { ...supplier.evidence, crown_jewels: ["Customer portal"], opencrq_asset_ids: ["asset-42"] },
      state: "case_opened",
      decision: {
        version: 1,
        outcome: "case",
        reason_code: "in_scope",
        reason_params: { criticality: "high", threshold: "high", confidence: "medium" },
        reconcile: { assets: [], crown_jewels_named: ["Customer portal"], tech_stack: [] },
        scope: { criticality: "high", threshold: "high", sources: [] },
      },
    };
    const facts = await renderTrace(dependent);
    const evidence = within(facts).queryByTestId("ctem-signal-evidence")?.textContent ?? "";
    expect(evidence).not.toContain("crown_jewels");
    expect(evidence).not.toContain("opencrq_asset_ids");
    expect(evidence).not.toContain("asset-42");
    expect(screen.getByTestId("ctem-decision-inputs")).toHaveTextContent("Customer portal");
  });
});
