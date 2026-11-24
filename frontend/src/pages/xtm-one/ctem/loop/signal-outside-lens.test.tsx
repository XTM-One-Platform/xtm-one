// SPDX-License-Identifier: Apache-2.0
// CTEM-26-UI: an outside-lens signal of the attack surface pass (CTEM-26)
// says, in named rows, the baseline difference that fired it: what the
// baseline held and what this pass observed, never raw evidence lines and
// never the pass's English reason twice.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, surfaceFacts } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000260";
const base = { source: "attack_surface", provider: "shodan", hostnames: ["portal.corp.example"] };

const newPort: CtemSignal = {
  id: ID,
  trigger_path: "new_exposure",
  trigger_path_label: "New exposure",
  lens: "outside",
  source: { provider: "shodan", external_id: "" },
  subject: { kind: "asset", ref: "portal.corp.example", label: "portal.corp.example: portal answers on 8443/tcp, not seen by the previous pass" },
  observed_at: "2026-11-22T06:00:00Z",
  first_seen_at: "2026-11-22T06:00:00Z",
  last_seen_at: "2026-11-22T06:00:00Z",
  seen_count: 1,
  evidence: {
    ...base,
    ips: ["203.0.113.10"],
    ports: ["443/tcp", "8443/tcp"],
    cert_names: ["portal.corp.example"],
    asset: "portal",
    new_port: "8443/tcp",
    previous_ports: ["443/tcp"],
    reason: "portal answers on 8443/tcp, not seen by the previous pass",
  },
  state: "case_opened",
  decision: {
    version: 1,
    outcome: "case",
    reason_code: "in_scope",
    reason_params: { criticality: "high", threshold: "high", confidence: "high" },
    reconcile: { assets: ["portal"], crown_jewels_named: [], tech_stack: [] },
    scope: { criticality: "high", threshold: "high", sources: [] },
  },
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

describe("outside-lens signals of the attack surface pass", () => {
  it("reads the difference that fired each path, and nothing for another source", () => {
    expect(surfaceFacts(newPort.evidence)?.diff).toEqual({ key: "new_port", port: "8443/tcp", previous: ["443/tcp"] });
    expect(surfaceFacts({ ...base, ports: ["22/tcp"], asset: "db", declared_internet_facing: "no" })?.diff).toEqual({
      key: "not_internet_facing",
      ports: ["22/tcp"],
    });
    expect(surfaceFacts({ ...base, baseline: "resolves", observed: "no record" })?.diff).toEqual({ key: "dangling" });
    expect(surfaceFacts({ ...base, ips: ["198.51.100.7"], owned_domain: "corp.example", address_ranges: ["203.0.113.0/24"] })?.diff).toEqual({
      key: "outside_ranges",
      domain: "corp.example",
      ip: "198.51.100.7",
    });
    expect(surfaceFacts({ ...base, last_seen_at: "2026-11-01T00:00:00Z", stale_after_days: 14 })?.diff).toEqual({
      key: "unseen",
      lastSeen: "2026-11-01T00:00:00Z",
      days: 14,
    });
    expect(surfaceFacts({ ...base, ports: ["80/tcp"], baseline: "absent" })?.diff).toEqual({ key: "new_host", ports: ["80/tcp"] });
    expect(surfaceFacts({ source: "opencti", new_port: "1/tcp" })).toBeNull();
    expect(surfaceFacts(null)).toBeNull();
  });

  it("the trace of a new port names the baseline and the observation, in named rows", async () => {
    const facts = await renderTrace(newPort);
    expect(within(facts).getByText("Previous pass saw 443/tcp")).toBeTruthy();
    expect(within(facts).getByText("Answers on 8443/tcp, a new port")).toBeTruthy();
    expect(within(facts).getByText("portal")).toBeTruthy();
    expect(within(facts).getByText("203.0.113.10")).toBeTruthy();
    expect(within(facts).getByText("443/tcp, 8443/tcp")).toBeTruthy();
    expect(within(facts).queryByTestId("ctem-signal-evidence")).toBeNull();
  });

  it("the trace of a stale host says when it was last seen, with a plural of 1", async () => {
    const stale: CtemSignal = {
      ...newPort,
      trigger_path: "stale_asset",
      trigger_path_label: "Stale or orphaned asset",
      evidence: { ...base, last_seen_at: "2026-11-01T00:00:00Z", stale_after_days: 1 },
    };
    const facts = await renderTrace(stale);
    expect(within(facts).getByText(/^The host last answered on /)).toBeTruthy();
    expect(within(facts).queryByText(/^Last seen on /)).toBeNull();
    expect(within(facts).getByText("Unseen for at least 1 day")).toBeTruthy();
    expect(facts).not.toHaveTextContent("last_seen_at");
  });
});
