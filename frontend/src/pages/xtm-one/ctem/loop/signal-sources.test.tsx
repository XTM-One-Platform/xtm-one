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
  silentReasonText,
  sourceIntegrationNames,
  sourceState,
  sourceStateIcon,
  SOURCE_STATE_LEGEND,
  type SourceState,
} from "./signal-sources";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);

/** Two connected GitHub integrations and one disconnected (#2736). */
const INTEGRATIONS = vi.hoisted(() => [
  { id: "gh-1", provider: "github", name: "GitHub Filigran", status: "connected" },
  { id: "gh-2", provider: "github", name: "GitHub Labs", status: "connected" },
  { id: "gh-3", provider: "github", name: "GitHub old", status: "disconnected" },
]);

vi.mock("@/hooks/api/useIntegrationApi", () => ({ useIntegrations: () => ({ data: INTEGRATIONS }) }));

function path(over: Partial<CtemSignalPath>): CtemSignalPath {
  return { id: "x", label: "X", default_lens: "inside", listener: false, counts: {}, total: 0, configured: false, ...over };
}

const PATHS: CtemSignalPath[] = [
  path({
    id: "new_vulnerability",
    label: "New vulnerability on a running version",
    default_lens: "world",
    status: "enabled",
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

describe("source state legend (UX round 1 on #2741, B3)", () => {
  it("names every state a chip can show exactly once, each entry with one icon that is its states' own", () => {
    const ALL: SourceState[] = ["error", "bound", "waiting", "excluded", "off", "not_configured", "not_set_up", "not_connected"];
    const named = SOURCE_STATE_LEGEND.flatMap((entry) => [...entry.states]);
    expect([...named].sort()).toEqual([...ALL].sort());
    for (const entry of SOURCE_STATE_LEGEND) {
      expect(new Set(entry.states.map((s) => sourceStateIcon(s).icon)).size).toBe(1);
    }
    const icons = SOURCE_STATE_LEGEND.map((entry) => sourceStateIcon(entry.states[0]).icon);
    expect(new Set(icons).size).toBe(icons.length);
  });
});

describe("signal source rules", () => {
  it("never reads connected without a bound source", () => {
    expect(pathStatus({ sources: [] })).toBe("agents_api");
    expect(pathStatus({ sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }] })).toBe("not_connected");
    expect(pathStatus({ sources: [{ provider: "aikido", kind: "listener", event: "", bound: true }] })).toBe("enabled");
    // #2436 (Sam, 26 Nov 2026): the status reads the sources' on/off state, never the connection.
    expect(pathStatus({ sources: [{ provider: "opencti", kind: "listener", event: "", bound: true }, { provider: "opencti", kind: "pir_playbook", event: "", bound: false, not_set_up: true }] })).toBe("partly_enabled");
    expect(pathStatus({ sources: [{ provider: "openaev", kind: "listener", event: "", bound: false, off: true }] })).toBe("off");
    expect(pathStatus({ sources: [{ provider: "shodan", kind: "pass", event: "", bound: true }, { provider: "gti", kind: "pass", event: "", bound: false, off: true, excluded: true }] })).toBe("enabled");
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

  it("never offers an off webhook source as Connect or Turn on: it is switched on from its own row (#2509 review)", () => {
    const hook = { provider: "webhook", kind: "webhook" as const, event: "", bound: false, off: true, key: "webhook:w-1", name: "Scanner" };
    expect(connectTarget({ sources: [hook] })).toBeNull();
    const listener = { provider: "aikido", kind: "listener" as const, event: "", bound: true };
    expect(connectTarget({ sources: [listener, hook] })).toBeNull();
    const unboundListener = { provider: "censys", kind: "listener" as const, event: "", bound: false };
    expect(connectTarget({ sources: [hook, unboundListener] })).toBe(unboundListener);
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
    expect(within(vulnRow).getByText("Enabled")).toBeTruthy();
    expect(within(vulnRow).queryByText("Connected")).toBeNull();
    expect(within(vulnRow).getByTestId("ctem-signal-sources-30d").textContent).toBe("4");
    // The status chip sits in its own column: one cell per row (Sam, 23 Nov 2026, #1344).
    expect(within(vulnRow).getAllByTestId("ctem-signal-sources-status")).toHaveLength(1);

    const agents = rows.find((r) => r.getAttribute("data-path") === "blind_spot") as HTMLElement;
    expect(within(agents).getByText("Agents and API only")).toBeTruthy();
    expect(within(agents).queryByText("Enabled")).toBeNull();
    expect(within(agents).queryByRole("button", { name: "Connect" })).toBeNull();

    const code = rows.find((r) => r.getAttribute("data-path") === "code_flaw") as HTMLElement;
    expect(within(code).getByText("Not connected")).toBeTruthy();
    expect(within(code).getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("agrees with the Discovery card on every path, whatever `configured` says (UX round 1 B1 on #1194)", async () => {
    const mixed: CtemSignalPath[] = [
      path({ id: "code_flaw", label: "Code flaw", configured: true, status: "not_connected", sources: [{ provider: "aikido", kind: "listener", event: "", bound: false }] }),
      path({ id: "third_party_event", label: "Third party", default_lens: "world", configured: false, status: "enabled", sources: [{ provider: "opencti", kind: "listener", event: "", bound: true }] }),
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
    expect(statusOf(cardRows)).toEqual({ code_flaw: "not_connected", third_party_event: "enabled", blind_spot: "agents_api" });
  });

  it("draws every source of a path as a chip with its state, never +N more (#2736, Sam 28 Nov 2026)", async () => {
    const outside = path({
      id: "new_exposure",
      label: "New exposure",
      default_lens: "outside",
      status: "enabled",
      sources: [
        { provider: "shodan", kind: "pass", event: "A new host or port", bound: true },
        { provider: "gti", kind: "pass", event: "A new host or port", bound: false },
      ],
    });
    const leak = path({
      id: "credential_leak",
      label: "Credential or secret leak",
      default_lens: "world",
      status: "enabled",
      sources: [
        { provider: "aikido", kind: "listener", event: "A leaked secret issue", bound: false },
        { provider: "opencti", kind: "listener", event: "A leak", bound: true },
        { provider: "github", kind: "listener", name: "secret_scanning", event: "A secret scanning alert", bound: true },
      ],
      // The backend counts what could be on here only: the row counts every source (#2736).
      sources_enabled: 2,
      sources_available: 2,
    });
    const code = path({
      id: "code_flaw",
      label: "Code or dependency flaw before production",
      status: "enabled",
      sources: [
        { provider: "aikido", kind: "listener", event: "A new issue", bound: false },
        { provider: "github", kind: "listener", event: "A Dependabot alert", bound: true },
        { provider: "github", kind: "listener", name: "code_scanning", event: "A code scanning alert", bound: true },
      ],
    });
    const decay = path({
      id: "control_decay",
      label: "Control or evidence decay",
      status: "off",
      sources: [{ provider: "opencrq", kind: "pass", event: "", bound: false, off: true }],
    });
    const vuln = path({
      id: "new_vulnerability",
      label: "New vulnerability on a running version",
      default_lens: "world",
      status: "enabled",
      sources: [
        { provider: "opencti", kind: "listener", event: "A new vulnerability", bound: true },
        { provider: "opencti", kind: "pir_playbook", event: "A PIR match", bound: false },
      ],
    });
    const all = [outside, leak, decay, vuln, code];
    const connected = connectedProviders(all);
    expect(sourceState(vuln.sources![1], connected)).toBe("not_set_up");
    expect(sourceState(outside.sources![1], connected)).toBe("not_connected");
    // UX advisory A2 of #2592: the cases recompute names the integrations it reads, never "None".
    const lines = [
      { provider: "opencti", kind: "listener", integration_name: "OpenCTI prod" },
      { provider: "opencti", kind: "pir_playbook", integration_name: "OpenCTI prod" },
      { provider: "openaev", kind: "listener", integration_name: "OpenAEV" },
    ];
    expect(sourceIntegrationNames({ provider: "opencti", kind: "cases_reader" }, lines)).toBe("OpenCTI prod");
    expect(sourceIntegrationNames({ provider: "opencti", kind: "pir_playbook" }, lines)).toBe("OpenCTI prod");
    expect(sourceIntegrationNames({ provider: "opencti", kind: "webhook" }, lines)).toBe("");
    // #2736: a GitHub listener is named by every connected GitHub integration, never "None" beside "On".
    expect(sourceIntegrationNames({ provider: "github", kind: "listener" }, lines, INTEGRATIONS)).toBe("GitHub Filigran, GitHub Labs");
    expect(sourceIntegrationNames({ provider: "opencti", kind: "listener" }, [], INTEGRATIONS)).toBe("");
    get.mockResolvedValue({ paths: all });
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    const row = (id: string) => rows.find((r) => r.getAttribute("data-path") === id) as HTMLElement;
    const chips = (id: string) =>
      within(row(id))
        .getAllByTestId("ctem-signal-source")
        .map((s) => `${within(s).getByTestId("ctem-signal-source-name").textContent}:${s.getAttribute("data-state")}`);

    // No "+N more" and no "+N not used here" anywhere in the view.
    expect(screen.queryByTestId("ctem-signal-sources-more")).toBeNull();
    expect(screen.queryByTestId("ctem-signal-sources-unused")).toBeNull();
    expect(document.body.textContent).not.toMatch(/\+\d+ more/);
    // Every source, the sources that are on first, a provider with no integration here last (dashed, muted).
    expect(chips("code_flaw")).toEqual(["GitHub Dependabot:bound", "GitHub Code scanning:bound", "Aikido Security:not_connected"]);
    expect(chips("credential_leak")).toEqual(["OpenCTI:bound", "GitHub Secret scanning:bound", "Aikido Security:not_connected"]);
    // The kind is in the tooltip, never on the chip, so a name fits whole (UX round 1 on #2741, B1, B2).
    expect(chips("new_exposure")).toEqual(["Shodan:bound", "Google Threat Intelligence:not_connected"]);
    expect(chips("new_vulnerability")).toEqual(["OpenCTI:bound", "OpenCTI PIR playbook:not_set_up"]);
    expect(chips("control_decay")).toEqual(["OpenCRQ:off"]);
    // The legend, once above the table (drawn from 8xl only, where the Sources column is: UX round 2 on #2741, A2).
    expect(screen.getAllByTestId("ctem-signal-sources-legend")).toHaveLength(1);
    // The Sources column holds the widest path (four chips) on two lines at 1728: 420 px, from 8xl (UX round 2 on #2741, B1).
    const sourcesHeader = screen.getAllByRole("columnheader").find((th) => th.textContent?.trim().startsWith("Sources") && !th.textContent?.includes("on"));
    expect(sourcesHeader?.style.width).toBe("420px");
    // A chip's icon is one a legend entry shows (UX round 1 on #2741, B3).
    const legendIcons = screen.getAllByTestId("ctem-signal-sources-legend-item").map((li) => li.getAttribute("data-icon"));
    expect(new Set(legendIcons).size).toBe(legendIcons.length);

    // "Sources on" counts every source of the path, the one not connected named in its tooltip.
    const leakCount = within(row("credential_leak")).getByTestId("ctem-signal-sources-on-count");
    expect(leakCount.textContent).toBe("2 of 3");
    expect(leakCount.getAttribute("data-missing-tip")).toBe("1 not connected: Aikido Security");
    expect(within(row("code_flaw")).getByTestId("ctem-signal-sources-on-count").textContent).toBe("2 of 3");
    expect(within(row("control_decay")).getByTestId("ctem-signal-sources-on-count").textContent).toBe("0 of 1");

    // The GitHub chip names the integrations that feed it in its tooltip.
    const dependabot = within(row("code_flaw")).getAllByTestId("ctem-signal-source")[0];
    expect(dependabot.getAttribute("aria-label")).toBe("GitHub Dependabot: On");
  });

  it("opens the path's sources on a click anywhere on the row, the Sources and count cells included (#2736)", async () => {
    const leak = path({
      id: "credential_leak",
      label: "Credential or secret leak",
      status: "enabled",
      count_30d: 3,
      sources: [
        { provider: "aikido", kind: "listener", event: "A leaked secret issue", bound: false },
        { provider: "opencti", kind: "listener", event: "A leak", bound: true },
      ],
    });
    get.mockResolvedValue({ paths: [leak] });
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const [row] = await screen.findAllByTestId("ctem-signal-sources-path");
    fireEvent.click(within(row).getAllByTestId("ctem-signal-source-name")[1]);
    expect(screen.getByTestId("ctem-path-sources").getAttribute("data-path")).toBe("credential_leak");
    fireEvent.click(screen.getByTestId("ctem-path-sources-close"));
    fireEvent.click(within(row).getByTestId("ctem-signal-sources-30d"));
    expect(screen.getByTestId("ctem-path-sources").getAttribute("data-path")).toBe("credential_leak");
  });

  it("says why a path that is on records nothing beside its 0 count, and nothing on a path with signals (#2642)", async () => {
    get.mockResolvedValue({
      paths: [
        PATHS[0],
        path({
          id: "stale_asset",
          label: "Stale or orphaned asset",
          status: "enabled",
          sources: [{ provider: "openaev", kind: "listener", event: "", bound: true }],
          count_30d: 0,
          silent_reason: {
            code: "upstream_sync_failed",
            detail: "An inventory sync failed",
            evaluation: { at: "2026-11-27T10:00:00+00:00", evaluated: 312, failed_syncs: 2 },
          },
        }),
      ],
    });
    renderWithProviders(
      <MemoryRouter>
        <SignalSourcesPanel />
      </MemoryRouter>,
    );
    const rows = await screen.findAllByTestId("ctem-signal-sources-path");
    const row = (id: string) => rows.find((r) => r.getAttribute("data-path") === id) as HTMLElement;
    const mark = within(row("stale_asset")).getByTestId("ctem-signal-sources-silent-reason");
    expect(mark.getAttribute("aria-label")).toContain("An inventory sync failed: its assets are not judged stale until it reads again");
    expect(mark.getAttribute("aria-label")).toContain("312 assets checked");
    expect(within(row("new_vulnerability")).queryByTestId("ctem-signal-sources-silent-reason")).toBeNull();
  });

  it("reads each silent reason code in plain words, never the API's English detail (#2642)", () => {
    const t = ((key: string) => key) as Parameters<typeof silentReasonText>[1];
    const fmt = (iso: string) => iso;
    expect(silentReasonText({ code: "never_evaluated", detail: "x" }, t, fmt)).toBe(
      "The inventory has not been evaluated yet: it is evaluated daily and after a purge",
    );
    expect(silentReasonText({ code: "listener_never_fired", detail: "x" }, t, fmt)).toBe("No source of this path has sent an event yet");
    expect(silentReasonText({ code: "no_asset_past_threshold", detail: "x" }, t, fmt)).toBe(
      "No asset is past the threshold and every synced asset still has its sync",
    );
    expect(silentReasonText({ code: "inventory_truncated", detail: "x" }, t, fmt)).toBe(
      "No checked asset is past the threshold, but the inventory is larger than one evaluation reads",
    );
    const unknown = { code: "some_future_code", detail: "English detail" } as unknown as Parameters<typeof silentReasonText>[0];
    expect(silentReasonText(unknown, t, fmt)).toBe("This path records no signal yet");
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

