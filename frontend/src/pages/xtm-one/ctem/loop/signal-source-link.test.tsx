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
      <MemoryRouter initialEntries={[`/?loop=signal:${ID}`]}>
        <SignalTraceBody id={ID} />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    const link = within(facts).getByRole("link");
    expect(link).toHaveAttribute("href", "https://aev.example/admin/scenarios/scn-1");
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("scenario_score_degraded");
    expect(evidence).not.toHaveTextContent("platform_url");
  });
});
