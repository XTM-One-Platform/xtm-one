// SPDX-License-Identifier: Apache-2.0
import { beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import { PirSelection } from "./PirSelection";

/**
 * Clear in the PIR picker saves the empty pick in one write (UX r2 B4 of #2454): the field used to
 * send one toggle per removed PIR, each saved from the same stale pick, so the last save won and
 * Clear dropped a single PIR.
 */

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = vi.fn();
});

const pir = (pir_id: string, name: string) => ({
  pir_id,
  integration_id: null,
  name,
  description: "",
  pir_type: "THREAT_LANDSCAPE",
  criteria_summary: "",
  question: "",
  owner: "",
  review_cadence: "",
  primary_assets: [],
  min_score: 50,
  enabled: true,
  last_seen_at: null,
  gone_at: null,
});

describe("PIR selection of a path (#2436)", () => {
  it("saves the empty pick once when Clear is pressed on a default of 3 PIRs", () => {
    const client = testQueryClient();
    client.setQueryData(["organization-context", "intelligence-policy"], {
      pirs: [pir("p1", "Ransomware"), pir("p2", "State actors"), pir("p3", "Supply chain")],
      total: 3,
    });
    const onSave = vi.fn();
    renderWithProviders(
      <MemoryRouter>
        <PirSelection path={{ id: "relevant_threat" } as CtemSignalPath} profile={undefined} isAdmin saving={false} onSave={onSave} />
      </MemoryRouter>,
      client,
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].signal_sources.pirs).toEqual({ relevant_threat: [] });
  });
});
