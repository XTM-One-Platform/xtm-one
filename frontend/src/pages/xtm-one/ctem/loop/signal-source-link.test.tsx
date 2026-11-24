// CTEM-15-UI (#956): an OpenAEV signal (a mitigation regression, path 8) opens
// its source run from the trace's Source row; its platform_url is the link,
// never an evidence row.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, signalSourceLink } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000008";

const regression: CtemSignal = {
  id: ID,
  trigger_path: "mitigation_regression",
  trigger_path_label: "Mitigation regression",
  lens: "inside",
  source: { provider: "openaev", external_id: "scn-1" },
  subject: { kind: "asset", ref: "openaev scenario scn-1", label: "Score of scenario Web tier dropped" },
  observed_at: "2026-11-21T08:00:00Z",
  first_seen_at: "2026-11-21T08:00:00Z",
  last_seen_at: "2026-11-21T08:00:00Z",
  seen_count: 1,
  evidence: {
    event: "scenario_score_degraded",
    resource_type: "scenario",
    platform_url: "https://aev.example/admin/scenarios/scn-1",
  },
  state: "confirmed",
  decision: null,
  case_id: null,
  recorded_via: "listener",
};

afterEach(() => get.mockReset());

describe("signal source link", () => {
  it("reads link first, then platform_url, and nothing else", () => {
    expect(signalSourceLink({ link: "https://a", platform_url: "https://b" })).toBe("https://a");
    expect(signalSourceLink({ platform_url: "https://b" })).toBe("https://b");
    expect(signalSourceLink({ platform_url: "" })).toBeUndefined();
    expect(signalSourceLink({ platform_url: 3 })).toBeUndefined();
    expect(signalSourceLink(null)).toBeUndefined();
  });

  it("an OpenAEV regression signal links its source run, not an evidence row", async () => {
    get.mockResolvedValue(regression);
    renderWithProviders(
      <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
        <SignalTraceBody id={ID} />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    const link = within(facts).getByRole("link");
    expect(link).toHaveAttribute("href", "https://aev.example/admin/scenarios/scn-1");
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("scenario_score_degraded");
    expect(evidence).not.toHaveTextContent("platform_url");
    expect(within(facts).getByTestId("ctem-signal-source")).toHaveAttribute("data-source", "openaev");
  });

  // #1357: an OpenCTI signal with no link in its evidence opens its entity in OpenCTI by its id,
  // its source drawn with the OpenCTI emblem; no base URL, no pivot.
  const cti: CtemSignal = {
    ...regression,
    trigger_path: "new_vulnerability",
    source: { provider: "opencti", external_id: "vulnerability--1" },
    subject: { kind: "vulnerability", ref: "CVE-2026-1", label: "CVE-2026-1" },
    evidence: { event: "create" },
  };

  it("an OpenCTI signal pivots to its entity in OpenCTI", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        path.startsWith("/ctem-metrics/posture") ? { products: { opencti: { present: true, url: "https://cti.example/" } } } : cti,
      ),
    );
    renderWithProviders(
      <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
        <SignalTraceBody id={ID} />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    expect(within(facts).getByTestId("ctem-signal-source")).toHaveAttribute("data-source", "opencti");
    const pivot = await within(facts).findByTestId("ctem-signal-source-pivot");
    expect(pivot).toHaveAttribute("href", "https://cti.example/dashboard/id/vulnerability--1");
    expect(pivot).toHaveAttribute("target", "_blank");
  });

  it("draws no pivot when OpenCTI is not connected", async () => {
    get.mockImplementation((path: string) => Promise.resolve(path.startsWith("/ctem-metrics/posture") ? { products: {} } : cti));
    renderWithProviders(
      <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
        <SignalTraceBody id={ID} />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    expect(within(facts).queryByTestId("ctem-signal-source-pivot")).toBeNull();
    expect(within(facts).getByTestId("ctem-signal-source")).toHaveTextContent("OpenCTI");
  });
});
