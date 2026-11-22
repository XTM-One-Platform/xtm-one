// The signal sources dialog (#1189): status derivation, Connect targets, and the dialog grouped by lens.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { SignalSourcesDialog } from "./SignalSourcesDialog";
import { connectHref, connectTarget, connectedCount, pathStatus } from "./signal-sources";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);

function path(over: Partial<CtemSignalPath>): CtemSignalPath {
  return { id: "x", label: "X", default_lens: "inside", listener: false, counts: {}, total: 0, configured: false, ...over };
}

const PATHS: CtemSignalPath[] = [
  path({
    id: "new_vulnerability",
    label: "New vulnerability on a running version",
    default_lens: "world",
    status: "connected",
    description: "A new or updated vulnerability that may affect what you run",
    sources: [
      { provider: "opencti", kind: "listener", event: "A vulnerability is created or updated, or linked to one of your assets", bound: true },
      { provider: "opencti", kind: "pir_playbook", event: "The PIR playbook flags a vulnerability in one of your PIRs", bound: false },
    ],
    count_30d: 4,
    total: 6,
    last_signal_at: "2026-11-20T10:00:00+00:00",
  }),
  path({
    id: "code_flaw",
    label: "Code or dependency flaw before production",
    status: "not_connected",
    sources: [{ provider: "aikido", kind: "listener", event: "A leaked secret issue", bound: false }],
  }),
  path({ id: "blind_spot", label: "Blind spot or coverage gap", status: "agents_api", sources: [] }),
];

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ paths: PATHS });
});

describe("signal source rules", () => {
  it("never reads connected without a bound source", () => {
    expect(pathStatus({ sources: [] })).toBe("agents_api");
    expect(pathStatus({ sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }] })).toBe("not_connected");
    expect(pathStatus({ sources: [{ provider: "aikido", kind: "listener", event: "", bound: true }] })).toBe("connected");
    expect(pathStatus({ status: "agents_api", sources: [] })).toBe("agents_api");
  });

  it("counts connected paths only, agents-only paths in the total", () => {
    expect(connectedCount(PATHS)).toBe(1);
  });

  it("sends Connect to the right page: a listener to its integration, a pass and the playbook to the organization profile", () => {
    expect(connectHref({ provider: "aikido", kind: "listener" })).toBe("/integrations/new?provider=aikido");
    expect(connectHref({ provider: "shodan", kind: "pass" })).toContain("/settings/organization");
    expect(connectHref({ provider: "opencti", kind: "pir_playbook" })).toContain("/settings/organization");
    expect(connectTarget(PATHS[0])?.kind).toBe("pir_playbook");
    expect(connectTarget(PATHS[2])).toBeNull();
  });
});

describe("SignalSourcesDialog", () => {
  it("lists every path from the endpoint by lens, with status, sources and the Connect action", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/?signal-sources=1"]}>
        <SignalSourcesDialog />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    expect(rows.map((r) => r.getAttribute("data-path")).sort()).toEqual(["blind_spot", "code_flaw", "new_vulnerability"]);
    expect(screen.getByText("1 of 3 trigger paths connected. Every signal starts the exposure management loop.")).toBeTruthy();

    const world = screen.getByTestId("ctem-signal-sources-lens-world");
    expect(within(world).getByText("Connected")).toBeTruthy();
    expect(within(world).getByText("4 signals in 30 days")).toBeTruthy();

    const agents = rows.find((r) => r.getAttribute("data-path") === "blind_spot") as HTMLElement;
    expect(within(agents).getByText("Agents and API only")).toBeTruthy();
    expect(within(agents).queryByText("Connected")).toBeNull();
    expect(within(agents).queryByRole("button", { name: "Connect" })).toBeNull();

    const code = rows.find((r) => r.getAttribute("data-path") === "code_flaw") as HTMLElement;
    expect(within(code).getByText("Not connected")).toBeTruthy();
    expect(within(code).getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("stays closed without the query parameter", () => {
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesDialog />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("ctem-signal-sources")).toBeNull();
  });
});
