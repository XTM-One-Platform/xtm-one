// SPDX-License-Identifier: Apache-2.0
// CTEM-27-UI (#1126): an OpenAEV stale-asset signal (path 6) says its
// freshness verdict in words in the loop trace, links its OpenAEV asset from
// the Source row, and keeps the event and resource keys out of the raw
// evidence. A plain OpenAEV asset update is no freshness verdict.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, freshnessFacts } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000270";

const stale: CtemSignal = {
  id: ID,
  trigger_path: "stale_asset",
  trigger_path_label: "Stale or orphaned asset",
  lens: "outside",
  source: { provider: "openaev", external_id: "a1" },
  subject: { kind: "asset", ref: "openaev asset a1", label: "web-01" },
  observed_at: "2026-11-22T08:00:00Z",
  first_seen_at: "2026-11-22T08:00:00Z",
  last_seen_at: "2026-11-22T08:00:00Z",
  seen_count: 1,
  evidence: {
    event: "asset_stale",
    resource_type: "asset",
    message: "[asset] web-01 updated",
    platform_url: "https://aev.example/admin/assets/endpoints/a1",
  },
  state: "confirmed",
  decision: null,
  case_id: null,
  recorded_via: "listener",
};

afterEach(() => get.mockReset());

describe("asset freshness facts", () => {
  it("reads a stale verdict from an OpenAEV asset_stale delivery only", () => {
    expect(freshnessFacts("openaev", stale.evidence)).toEqual([{ key: "stale" }]);
    expect(freshnessFacts("openaev", { event: "asset_updated" })).toEqual([]);
    expect(freshnessFacts("opencti", stale.evidence)).toEqual([]);
    expect(freshnessFacts("openaev", null)).toEqual([]);
  });

  it("the trace names the stale verdict, links the asset and leaves the event keys out of the evidence", async () => {
    get.mockResolvedValue(stale);
    renderWithProviders(
      <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
        <SignalTraceBody id={ID} />
      </MemoryRouter>,
    );
    const facts = await screen.findByTestId("ctem-signal-facts");
    expect(within(facts).getByText("Freshness")).toBeTruthy();
    expect(within(facts).getByText("Stale: not seen by OpenAEV past the tenant's threshold")).toBeTruthy();
    expect(within(facts).getByRole("link")).toHaveAttribute("href", "https://aev.example/admin/assets/endpoints/a1");
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("web-01 updated");
    expect(evidence).not.toHaveTextContent("asset_stale");
    expect(evidence).not.toHaveTextContent("resource_type");
  });
});
