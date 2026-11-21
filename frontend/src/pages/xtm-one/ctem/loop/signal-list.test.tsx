// #877 UX round 1 B2: the signal list of the loop trace names "n of N" on its
// filter row, scrolls on its own and reaches every signal with "Load more".

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { LoopTraceDialog } from "./LoopTraceDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function signal(n: number): CtemSignal {
  return {
    id: `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`,
    trigger_path: "new_exposure",
    trigger_path_label: "New exposure",
    lens: "outside",
    source: { provider: "shodan", external_id: `host-${n}` },
    subject: { kind: "asset", ref: `host-${n}.example.test`, label: `host-${n}.example.test` },
    observed_at: "2026-11-21T08:00:00Z",
    first_seen_at: "2026-11-21T08:00:00Z",
    last_seen_at: "2026-11-21T08:05:00Z",
    seen_count: 1,
    evidence: {},
    state: "to_validate",
    decision: null,
    case_id: null,
    recorded_via: "api",
  };
}

function page(url: string) {
  const offset = Number(new URL(url, "http://x").searchParams.get("offset") ?? 0);
  const total = 70;
  const signals = Array.from({ length: Math.min(50, total - offset) }, (_, i) => signal(offset + i));
  return { total, counts: { to_validate: total }, signals };
}

afterEach(() => get.mockReset());

describe("signal list", () => {
  it("shows n of N on the filter row and loads the next page", async () => {
    get.mockImplementation(async (url: string) => page(url));
    renderWithProviders(
      <MemoryRouter initialEntries={["/?loop=signals:to_validate"]}>
        <LoopTraceDialog />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("ctem-signal-shown")).toHaveTextContent("Showing 50 of 70");
    expect(screen.getAllByTestId("ctem-signal-row")).toHaveLength(50);
    fireEvent.click(screen.getByTestId("ctem-signal-load-more"));
    await waitFor(() => expect(screen.getAllByTestId("ctem-signal-row")).toHaveLength(70));
    expect(get).toHaveBeenCalledWith(expect.stringContaining("offset=50"));
    expect(screen.getByTestId("ctem-signal-shown")).toHaveTextContent("Showing 70 of 70");
    expect(screen.queryByTestId("ctem-signal-load-more")).toBeNull();
  });
});
