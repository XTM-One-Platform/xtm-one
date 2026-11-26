// The signal sources (#1189, #1263): status derivation, Connect targets, the Sources tab grouped by lens, the trigger paths card's button.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { SignalSourcesPanel } from "./SignalSourcesPanel";
import { TriggerPathsCard } from "./TriggerPathsCard";
import {
  connectHref,
  connectNeedsAdmin,
  connectTarget,
  connectedCount,
  connectedProviders,
  pathStatus,
  sourceState,
  splitSources,
} from "./signal-sources";

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
    // A pass whose provider is connected but which does not run reads Off, never Not connected (#1344).
    expect(pathStatus({ sources: [{ provider: "opencrq", kind: "pass", event: "", bound: false, off: true }] })).toBe("off");
  });

  it("counts connected paths only, agents-only paths in the total", () => {
    expect(connectedCount(PATHS)).toBe(1);
  });

  it("sends Connect to the right page: a listener to its integration, a pass and the playbook to their inventory card", () => {
    expect(connectHref({ provider: "aikido", kind: "listener" })).toBe("/integrations/new?provider=aikido");
    // The outside lens card lives on CTEM > Inventory (#1178).
    expect(connectHref({ provider: "shodan", kind: "pass" })).toContain("/ctem/inventory");
    // The PIR list lives on the Intelligence policy card of CTEM > Inventory > Exposure policy (#2435).
    expect(connectHref({ provider: "opencti", kind: "pir_playbook" })).toBe("/ctem/inventory/exposure?card=intelligence_policy");
    expect(connectTarget(PATHS[0])?.kind).toBe("pir_playbook");
    expect(connectTarget(PATHS[2])).toBeNull();
  });

  it("sends the OpenCRQ pass to the evidence health card, never to a new OpenCRQ integration (#1344)", () => {
    expect(connectHref({ provider: "opencrq", kind: "pass" })).toBe("/ctem/inventory/exposure?card=evidence_health");
    expect(connectHref({ provider: "opencrq", kind: "listener" })).toBe("/integrations/new?provider=opencrq");
    const off = { provider: "opencrq", kind: "pass" as const, event: "", bound: false, off: true };
    expect(connectTarget({ sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }, off] })).toBe(off);
  });

  it("keeps Turn on of a profile pass for administrators: a reader gets no button for it (UX round 1 on #1356, B3)", () => {
    expect(connectNeedsAdmin({ provider: "opencrq", kind: "pass" })).toBe(true);
    expect(connectNeedsAdmin({ provider: "shodan", kind: "pass" })).toBe(true);
    expect(connectNeedsAdmin({ provider: "opencti", kind: "pir_playbook" })).toBe(true);
    expect(connectNeedsAdmin({ provider: "aikido", kind: "listener" })).toBe(false);
  });
});

function Probe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

describe("SignalSourcesPanel", () => {
  it("lists every path from the endpoint by lens, with status, sources and the Connect action", async () => {
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    expect(rows.map((r) => r.getAttribute("data-path")).sort()).toEqual(["blind_spot", "code_flaw", "new_vulnerability"]);
    expect(screen.queryByRole("dialog")).toBeNull();

    // Grouped by lens: a group header row per lens, with its help line.
    expect(screen.getByTestId("ctem-signal-sources-lens-world").textContent).toContain("World");
    const vulnRow = rows.find((r) => r.getAttribute("data-path") === "new_vulnerability") as HTMLElement;
    expect(within(vulnRow).getByText("Connected")).toBeTruthy();
    expect(within(vulnRow).getByTestId("ctem-signal-sources-30d").textContent).toBe("4");
    // The status chip sits in its own column: one cell per row (Sam, 23 Nov 2026, #1344).
    expect(within(vulnRow).getAllByTestId("ctem-signal-sources-status")).toHaveLength(1);

    const agents = rows.find((r) => r.getAttribute("data-path") === "blind_spot") as HTMLElement;
    expect(within(agents).getByText("Agents and API only")).toBeTruthy();
    expect(within(agents).queryByText("Connected")).toBeNull();
    expect(within(agents).queryByRole("button", { name: "Connect" })).toBeNull();

    const code = rows.find((r) => r.getAttribute("data-path") === "code_flaw") as HTMLElement;
    expect(within(code).getByText("Not connected")).toBeTruthy();
    expect(within(code).getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("agrees with the Discovery card on every path, whatever `configured` says (UX round 1 B1 on #1194)", async () => {
    const mixed: CtemSignalPath[] = [
      path({ id: "code_flaw", label: "Code flaw", configured: true, status: "not_connected", sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }] }),
      path({ id: "third_party_event", label: "Third party", default_lens: "world", configured: false, status: "connected", sources: [{ provider: "opencti", kind: "listener", event: "", bound: true }] }),
      path({ id: "blind_spot", label: "Blind spot", default_lens: "outside", configured: false, status: "agents_api", sources: [] }),
    ];
    get.mockResolvedValue({ paths: mixed });
    renderWithProviders(
      <MemoryRouter>
        <TriggerPathsCard />
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const panelRows = await screen.findAllByTestId("ctem-signal-sources-path");
    const cardRows = await screen.findAllByTestId("ctem-trigger-path");
    const statusOf = (rows: HTMLElement[]) => Object.fromEntries(rows.map((r) => [r.getAttribute("data-path"), r.getAttribute("data-status")]));
    expect(statusOf(cardRows)).toEqual(statusOf(panelRows));
    expect(statusOf(cardRows)).toEqual({ code_flaw: "not_connected", third_party_event: "connected", blind_spot: "agents_api" });
  });

  it("lists only what feeds a path here: an unconnected provider is folded into +N possible, never beside a bound one (#1401)", async () => {
    const outside = path({
      id: "new_exposure",
      label: "New exposure",
      default_lens: "outside",
      status: "connected",
      sources: [
        { provider: "shodan", kind: "pass", event: "A new host or port", bound: true },
        { provider: "gti", kind: "pass", event: "A new host or port", bound: false },
      ],
    });
    const leak = path({
      id: "credential_leak",
      label: "Credential or secret leak",
      default_lens: "world",
      status: "connected",
      sources: [
        { provider: "aikido", kind: "listener", event: "A leaked secret issue", bound: false },
        { provider: "opencti", kind: "listener", event: "A leak", bound: true },
      ],
    });
    const decay = path({
      id: "control_decay",
      label: "Control or evidence decay",
      status: "off",
      sources: [{ provider: "opencrq", kind: "pass", event: "", bound: false, off: true }],
    });
    // OpenCTI is connected (its listener is bound): its PIR playbook not provisioned is "Not set up", never possible.
    const vuln = path({
      id: "new_vulnerability",
      label: "New vulnerability on a running version",
      default_lens: "world",
      status: "connected",
      sources: [
        { provider: "opencti", kind: "listener", event: "A new vulnerability", bound: true },
        { provider: "opencti", kind: "pir_playbook", event: "A PIR match", bound: false },
      ],
    });
    const all = [outside, leak, decay, vuln, PATHS[1]];
    const connected = connectedProviders(all);
    expect(sourceState(vuln.sources![1], connected)).toBe("not_set_up");
    expect(sourceState(outside.sources![1], connected)).toBe("not_connected");
    expect(splitSources(outside, connected).live.map((s) => s.provider)).toEqual(["shodan"]);
    expect(splitSources(outside, connected).possible.map((s) => s.provider)).toEqual(["gti"]);
    expect(splitSources(vuln, connected).possible).toHaveLength(0);
    expect(splitSources(decay, connected).live).toHaveLength(1);
    get.mockResolvedValue({ paths: all });
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    const row = (id: string) => rows.find((r) => r.getAttribute("data-path") === id) as HTMLElement;
    const listed = (id: string) => within(row(id)).getAllByTestId("ctem-signal-source").map((s) => s.getAttribute("data-provider"));

    expect(listed("new_exposure")).toEqual(["shodan"]);
    expect(within(row("new_exposure")).getByTestId("ctem-signal-sources-possible").getAttribute("data-providers")).toBe("gti");
    expect(within(row("new_exposure")).getByTestId("ctem-signal-sources-possible").textContent).toBe("+1 possible");
    expect(listed("credential_leak")).toEqual(["opencti"]);
    expect(within(row("credential_leak")).getByTestId("ctem-signal-sources-possible").getAttribute("data-providers")).toBe("aikido");
    // The compact line under 4xl says the same: the bound source, then the count of possible ones.
    expect(within(row("credential_leak")).getByTestId("ctem-signal-sources-compact").textContent).not.toContain("Aikido");
    expect(within(row("credential_leak")).getByTestId("ctem-signal-sources-compact-possible").textContent).toBe("+1 possible");

    // A pass turned off is kept, marked Off, with its Turn on.
    const off = within(row("control_decay")).getByTestId("ctem-signal-source");
    expect(off.getAttribute("data-off")).toBe("true");
    expect(off.textContent).toContain("Off");
    expect(within(row("control_decay")).queryByTestId("ctem-signal-sources-possible")).toBeNull();

    // A source of a connected provider not set up is listed with its own words, never folded as "Not connected".
    const vulnSources = within(row("new_vulnerability")).getAllByTestId("ctem-signal-source");
    expect(vulnSources.map((s) => s.getAttribute("data-state"))).toEqual(["bound", "not_set_up"]);
    expect(vulnSources[1].textContent).toContain("Not set up");
    expect(within(row("new_vulnerability")).queryByTestId("ctem-signal-sources-possible")).toBeNull();
    expect(within(row("new_vulnerability")).getByTestId("ctem-signal-sources-compact").textContent).not.toContain("possible");

    // Nothing live: the possible sources stay listed, muted, under Not connected, with Connect on the first.
    const code = within(row("code_flaw")).getByTestId("ctem-signal-source");
    expect(code.getAttribute("data-possible")).toBe("true");
    expect(within(row("code_flaw")).getByText("Not connected")).toBeTruthy();
    expect(within(row("code_flaw")).getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("opens the Signals tab on the path clicked, never the whole list (davidloiret review on #1194)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/signals/sources"]}>
        <SignalSourcesPanel />
        <Probe />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    const vuln = rows.find((r) => r.getAttribute("data-path") === "new_vulnerability") as HTMLElement;
    fireEvent.click(within(vuln).getByTestId("ctem-signal-sources-path-open"));
    expect(screen.getByTestId("location").textContent).toBe("/ctem/signals?s_f_path=new_vulnerability");
  });
});

