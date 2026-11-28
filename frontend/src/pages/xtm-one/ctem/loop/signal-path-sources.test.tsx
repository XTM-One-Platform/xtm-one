// The sources of one trigger path managed from the Sources tab (#1445): exclusion rules and the side panel.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { SignalPathSourcesSheet } from "./SignalPathSourcesSheet";
import { canExclude, connectTarget, connectedProviders, pathStatus, profilePassPaths, sourceState, withExclusion } from "./signal-sources";

const state = vi.hoisted(() => ({ admin: true, saved: [] as unknown[] }));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.admin }) }));
vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useIntegrations: () => ({
    data: [
      { id: "i-shodan", name: "Shodan prod", provider: "shodan", status: "connected" },
      { id: "i-opencrq", name: "OpenCRQ risk", provider: "opencrq", status: "connected" },
      { id: "i-gh-1", name: "GitHub Filigran", provider: "github", status: "connected" },
      { id: "i-gh-2", name: "GitHub Labs", provider: "github", status: "connected" },
    ],
  }),
}));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({
    data: {
      version: 3,
      profile: {
        name: "Acme",
        owned_domains: ["acme.example"],
        attack_surface: { enabled: true, integration: "i-shodan", integrations: [], cadence_hours: 24, stale_after_days: 30 },
        signal_sources: { excluded: { new_exposure: ["gti:pass"] } },
      },
    },
  }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: (body: unknown) => state.saved.push(body) }),
}));

const EXPOSURE: CtemSignalPath = {
  id: "new_exposure",
  label: "New exposure",
  default_lens: "outside",
  listener: false,
  counts: {},
  total: 0,
  configured: true,
  status: "enabled",
  sources: [
    { provider: "shodan", kind: "pass", event: "A new host or port", bound: true, available: true, also_feeds: ["stale_asset", "surface_drift"] },
    { provider: "gti", kind: "pass", event: "A new host or port", bound: false, excluded: true, available: true, also_feeds: ["stale_asset"] },
    { provider: "censys", kind: "listener", event: "A new host", bound: false },
  ],
};
const OTHERS: CtemSignalPath[] = [
  { ...EXPOSURE, id: "stale_asset", label: "Stale or orphaned asset", sources: [] },
  { ...EXPOSURE, id: "surface_drift", label: "Attack-surface drift", sources: [] },
];

function open(path: CtemSignalPath = EXPOSURE) {
  const paths = [path, ...OTHERS];
  const view = renderWithProviders(
    <MemoryRouter>
      <SignalPathSourcesSheet path={path} paths={paths} connected={connectedProviders(paths)} onClose={() => undefined} />
    </MemoryRouter>,
  );
  // #2506: the panel is a table; each row opens its configuration from its Configure button.
  for (const button of screen.queryAllByTestId("ctem-path-source-configure")) fireEvent.click(button);
  return view;
}

/** The rows of the table by provider, in the order of the path's sources. */
function rowsBy(path: CtemSignalPath = EXPOSURE) {
  const rows = screen.getAllByTestId("ctem-path-source-row");
  return (path.sources ?? []).map((s) => rows.find((r) => r.getAttribute("data-provider") === s.provider) as HTMLElement);
}

/** The opened configuration of one source, by provider. */
function configOf(provider: string): HTMLElement {
  return screen.getAllByTestId("ctem-path-source").find((c) => c.getAttribute("data-provider") === provider) as HTMLElement;
}

beforeEach(() => {
  state.admin = true;
  state.saved = [];
});

describe("per path exclusion rules (#1445)", () => {
  it("adds and removes one source of one path, keeping the others", () => {
    const start = { excluded: { new_exposure: ["gti:pass"], control_decay: ["opencrq:pass"] } };
    expect(withExclusion(start, "new_exposure", "shodan:pass", true).excluded).toEqual({
      new_exposure: ["gti:pass", "shodan:pass"],
      control_decay: ["opencrq:pass"],
    });
    expect(withExclusion(start, "new_exposure", "gti:pass", false).excluded).toEqual({ control_decay: ["opencrq:pass"] });
    expect(withExclusion(undefined, "code_flaw", "aikido:listener", true).excluded).toEqual({ code_flaw: ["aikido:listener"] });
  });

  it("reads an excluded source as excluded, its provider still connected, and never offers to connect it", () => {
    const only = { sources: [{ provider: "gti", kind: "pass" as const, event: "", bound: false, excluded: true, available: true }] };
    expect(pathStatus(only)).toBe("not_connected");
    expect(connectedProviders([only]).has("gti")).toBe(true);
    expect(sourceState(only.sources[0], new Set(["gti"]))).toBe("excluded");
    expect(connectTarget(only)).toBeNull();
    expect(canExclude(only.sources[0])).toBe(true);
    expect(canExclude({ provider: "censys", kind: "listener", event: "", bound: false })).toBe(false);
  });
});

describe("the paths a Settings pass stops (#2784)", () => {
  it("lists every path one of whose sources is that pass, and no other", () => {
    const paths = [EXPOSURE, ...OTHERS, { ...EXPOSURE, id: "coverage_gap", label: "Blind spot", sources: [{ provider: "opencrq", kind: "pass" as const, event: "", bound: true }] }];
    expect(profilePassPaths(paths, "attack_surface")).toEqual([{ path: "new_exposure", label: "New exposure" }]);
    expect(profilePassPaths(paths, "evidence_health")).toEqual([{ path: "coverage_gap", label: "Blind spot" }]);
  });
});

describe("the sources side panel (#1445)", () => {
  it("lists every source with its state and the control that exists for it", () => {
    open();
    expect(rowsBy().map((r) => r.getAttribute("data-state"))).toEqual(["bound", "excluded", "not_connected"]);
    const [shodan, gti, censys] = [configOf("shodan"), configOf("gti"), configOf("censys")];
    expect(within(shodan).getByTestId("ctem-path-source-runs-on").textContent).toContain("Shodan prod");
    // #2740: the paths the pass feeds read as chips, this path first and marked.
    const feeds = within(shodan).getAllByTestId("ctem-path-source-feed");
    expect(feeds[0].getAttribute("data-current")).toBe("true");
    expect(feeds.map((f) => f.textContent).slice(1)).toEqual(["Stale or orphaned asset", "Attack-surface drift"]);
    expect(within(shodan).getByTestId("ctem-path-source-integrations")).toContainElement(within(shodan).getByTestId("ctem-path-source-pass"));
    expect(within(shodan).getByTestId("ctem-path-source-paths")).toContainElement(within(shodan).getByTestId("ctem-path-source-use"));
    expect(within(gti).getByTestId("ctem-path-source-use").getAttribute("aria-checked")).toBe("false");
    expect(within(censys).getByTestId("ctem-path-source-connect")).toBeTruthy();
    expect(within(censys).queryByTestId("ctem-path-source-use")).toBeNull();
  });

  it("puts an excluded source back on the path through the organization profile", () => {
    open();
    const gti = configOf("gti");
    fireEvent.click(within(gti).getByTestId("ctem-path-source-use"));
    expect(state.saved).toHaveLength(1);
    const body = state.saved[0] as { fields: Record<string, unknown> };
    expect(body.fields.signal_sources).toEqual({ excluded: {} });
    // #1752: the changed field alone travels, never the rest of the profile or the asset rows.
    expect(Object.keys(body.fields)).toEqual(["signal_sources"]);
  });

  it("takes one source off this path without turning its pass off", () => {
    open();
    const shodan = configOf("shodan");
    fireEvent.click(within(shodan).getByTestId("ctem-path-source-use"));
    const body = state.saved[0] as { fields: { signal_sources: { excluded: Record<string, string[]> }; attack_surface?: unknown } };
    expect(body.fields.signal_sources.excluded.new_exposure).toEqual(["gti:pass", "shodan:pass"]);
    // The pass stays on: its field is not sent at all.
    expect(body.fields.attack_surface).toBeUndefined();
  });

  it("turns the evidence health pass off on the single connected OpenCRQ, naming it so the off holds (#1937)", () => {
    const decay: CtemSignalPath = {
      ...EXPOSURE,
      id: "coverage_gap",
      label: "Blind spot or coverage gap",
      sources: [{ provider: "opencrq", kind: "pass", event: "A scenario with no evidence", bound: true, available: true, also_feeds: ["stale_asset"] }],
    };
    open(decay);
    const row = screen.getByTestId("ctem-path-source");
    // None declared: the pass runs on the single connected OpenCRQ, and says so.
    expect(within(row).getByTestId("ctem-path-source-runs-on").textContent).toContain("OpenCRQ risk");
    const pass = within(row).getByTestId("ctem-path-source-pass");
    expect(pass.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(pass);
    // #2784: the pass feeds two paths, so the off asks first and names both.
    expect(state.saved).toHaveLength(0);
    const list = screen.getByTestId("ctem-confirm-paths-off-list");
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Blind spot or coverage gap", "Stale or orphaned asset"]);
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    const body = state.saved[0] as { fields: Record<string, { enabled: boolean; integration: string }> };
    // `enabled: false` with none declared is the legacy default read as on: the off names the integration.
    expect(body.fields.evidence_health).toMatchObject({ enabled: false, integration: "i-opencrq" });
    expect(Object.keys(body.fields)).toEqual(["evidence_health"]);
  });

  it("asks before the scheduled pass goes off on several paths, and Cancel keeps it on (#2784)", () => {
    open();
    const shodan = configOf("shodan");
    fireEvent.click(within(shodan).getByTestId("ctem-path-source-pass"));
    expect(screen.getByText("Turn off the scheduled pass?")).toBeTruthy();
    const list = screen.getByTestId("ctem-confirm-paths-off-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(state.saved).toHaveLength(0);
    expect(screen.queryByTestId("ctem-confirm-paths-off-list")).toBeNull();
    // The note under the chips names its switch.
    expect(within(shodan).getByTestId("ctem-path-source-shared").textContent).toContain("Turning off the scheduled pass stops all of them.");
  });

  it("turns a source off one path without asking: Use on this path stops one path alone (#2784)", () => {
    open();
    fireEvent.click(within(configOf("shodan")).getByTestId("ctem-path-source-use"));
    expect(screen.queryByTestId("ctem-confirm-paths-off-list")).toBeNull();
    expect(state.saved).toHaveLength(1);
  });

  it("opens another path from its chip with the same source opened; the current chip is no button (#2784)", () => {
    const opened: string[] = [];
    const paths = [EXPOSURE, ...OTHERS];
    renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={EXPOSURE} paths={paths} connected={connectedProviders(paths)} onClose={() => undefined} onOpenPath={(id) => opened.push(id)} />
      </MemoryRouter>,
    );
    fireEvent.click(rowsBy()[0]);
    const feeds = within(configOf("shodan")).getAllByTestId("ctem-path-source-feed");
    expect(feeds[0].tagName).toBe("SPAN");
    expect(feeds[0].getAttribute("aria-current")).toBe("true");
    const stale = within(configOf("shodan")).getByRole("button", { name: "Open Stale or orphaned asset" });
    fireEvent.click(stale);
    expect(opened).toEqual(["stale_asset"]);
  });

  it("shows a reader the same states without controls that write", () => {
    state.admin = false;
    open();
    expect(screen.getByTestId("ctem-path-sources-read-only")).toBeTruthy();
    for (const control of [...screen.getAllByTestId("ctem-path-source-use"), ...screen.getAllByTestId("ctem-path-source-pass")]) {
      expect(control.hasAttribute("disabled")).toBe(true);
    }
    fireEvent.click(screen.getAllByTestId("ctem-path-source-use")[0]);
    expect(state.saved).toHaveLength(0);
  });

  it("gives a reader Connect on an integration source only, never on a profile pass (UX r1 #1461 B1)", () => {
    state.admin = false;
    const path: CtemSignalPath = {
      ...EXPOSURE,
      sources: [
        { provider: "gti", kind: "pass", event: "A new host or port", bound: false },
        { provider: "censys", kind: "listener", event: "A new host", bound: false },
      ],
    };
    open(path);
    const [gti, censys] = [configOf("gti"), configOf("censys")];
    expect(gti.getAttribute("data-state")).toBe("not_connected");
    expect(within(gti).queryByTestId("ctem-path-source-connect")).toBeNull();
    expect(within(censys).getByTestId("ctem-path-source-connect")).toBeTruthy();
    state.admin = true;
  });

  it("marks the current path chip by tone and a check, never by a longer label (UX r1 #2743 B1)", () => {
    open();
    const current = within(configOf("shodan")).getAllByTestId("ctem-path-source-feed")[0];
    expect(current.getAttribute("aria-current")).toBe("true");
    expect(current.querySelector(".sr-only")?.textContent).toBe("This path");
    expect(current.textContent).not.toContain("(this path)");
  });

  it("draws no empty Integrations section for a pass XTM One runs itself (UX r1 #2743 B2)", () => {
    const stale: CtemSignalPath = {
      ...EXPOSURE,
      id: "stale_asset",
      label: "Stale or orphaned asset",
      sources: [{ provider: "xtm_one", kind: "pass", event: "An asset no sync has seen", bound: true, available: true, also_feeds: ["surface_drift"] }],
    };
    open(stale);
    const row = configOf("xtm_one");
    expect(within(row).queryByTestId("ctem-path-source-integrations")).toBeNull();
    expect(within(row).getByTestId("ctem-path-source-paths")).toBeTruthy();
  });

  it("puts every switch next to its label, the same component in every section (UX r1 #2743 B3)", () => {
    open();
    const shodan = configOf("shodan");
    for (const [id, label] of [
      ["ctem-path-source-pass", "Scheduled pass on"],
      ["ctem-path-source-use", "Use on this path"],
    ]) {
      const control = within(shodan).getByTestId(id);
      expect(control.closest("label")).toHaveTextContent(label);
      expect(within(shodan).getByTestId(`${id}-row`)).toContainElement(control);
    }
  });

  it("moves focus into the panel, keeps Tab inside it and gives focus back on close (UX r1 #1461 B2)", () => {
    const opener = document.createElement("button");
    opener.textContent = "row";
    document.body.appendChild(opener);
    opener.focus();
    const view = open();
    const dialog = screen.getByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);
    const close = screen.getByTestId("ctem-path-sources-close");
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"));
    const last = focusables[focusables.length - 1];
    last.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(focusables[0]);
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    expect(close).toBeTruthy();
    view.unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("names every integration feeding a GitHub listener, says No integration in words, and opens a row on a click anywhere (#2736)", () => {
    const code: CtemSignalPath = {
      ...EXPOSURE,
      id: "code_flaw",
      label: "Code or dependency flaw before production",
      default_lens: "inside",
      sources: [
        { provider: "github", kind: "listener", event: "A Dependabot alert is opened", bound: true },
        { provider: "aikido", kind: "listener", event: "A new issue", bound: false },
      ],
    };
    const paths = [code, ...OTHERS];
    renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={code} paths={paths} connected={connectedProviders(paths)} onClose={() => undefined} />
      </MemoryRouter>,
    );
    const rows = screen.getAllByTestId("ctem-path-source-row");
    const github = rows.find((r) => r.getAttribute("data-provider") === "github") as HTMLElement;
    const aikido = rows.find((r) => r.getAttribute("data-provider") === "aikido") as HTMLElement;
    // A list in a narrow cell: the first name and +N, never a comma run (UX round 1 on #2741, B4).
    expect(within(github).getByTestId("ctem-path-source-integration-first").textContent).toBe("GitHub Filigran");
    expect(within(github).getByTestId("ctem-path-source-integration-more").textContent).toBe("+1");
    // The drawer names the source as the list chip does (UX advisory A1 on #2741); the Kind cell holds the kind word alone.
    expect(within(github).getByTestId("ctem-path-source-name").textContent).toBe("GitHub Dependabot");
    expect(within(github).getByTestId("ctem-path-source-kind").textContent).toBe("Live listener");
    expect(within(aikido).getByTestId("ctem-path-source-integration").textContent).toBe("No integration");
    // A click on the name cell opens the row's configuration in place; the toggle is a chevron with aria-expanded.
    expect(screen.queryAllByTestId("ctem-path-source")).toHaveLength(0);
    fireEvent.click(within(github).getByTestId("ctem-path-source-name"));
    expect(screen.getAllByTestId("ctem-path-source")).toHaveLength(1);
    expect(within(github).getByTestId("ctem-path-source-configure").getAttribute("aria-expanded")).toBe("true");
  });

  const GH = (over: Partial<NonNullable<CtemSignalPath["sources"]>[number]> = {}) => ({
    provider: "github",
    kind: "listener" as const,
    key: "github:listener",
    event: "A Dependabot alert is opened",
    bound: true,
    integrations: [
      { id: "i-gh-1", name: "GitHub Filigran", status: "connected", webhook_secret_set: true, last_delivery_at: "2026-11-28T09:00:00Z", used: true },
      { id: "i-gh-2", name: "GitHub Labs", status: "connected", webhook_secret_set: false, last_delivery_at: null, used: true },
    ],
    ...over,
  });
  const codePath = (source: ReturnType<typeof GH>): CtemSignalPath => ({ ...EXPOSURE, id: "code_flaw", label: "Code flaw", sources: [source] });

  it("lists each integration of a webhook listener with its state, last delivery and its own switch, and writes the exclusion (#2736)", () => {
    const path = codePath(GH());
    open(path);
    const config = configOf("github");
    const rows = within(config).getAllByTestId("ctem-path-source-integration-row");
    expect(rows.map((r) => r.getAttribute("data-state"))).toEqual(["connected", "waiting"]);
    expect(within(rows[0]).getByTestId("ctem-path-source-integration-delivery").textContent).not.toContain("Never");
    expect(within(rows[1]).getByTestId("ctem-path-source-integration-delivery").textContent).toContain("Never");
    // The source reads its event on line 2 of the Source cell.
    expect(screen.getByTestId("ctem-path-source-event").textContent).toBe("A Dependabot alert is opened");
    fireEvent.click(within(rows[1]).getByTestId("ctem-path-source-integration-use"));
    const body = state.saved[0] as { fields: { signal_sources: Record<string, unknown> } };
    expect(body.fields.signal_sources.excluded_integrations).toEqual({ "github:listener": ["i-gh-2"] });
    // The other lists travel whole.
    expect(body.fields.signal_sources.excluded).toEqual({ new_exposure: ["gti:pass"] });
  });

  it("reads On only when a used integration can deliver: Waiting for webhook without a secret, On, no delivery yet before any (#2736)", () => {
    const waiting = codePath(GH({ integrations: [{ id: "i-gh-2", name: "GitHub Labs", status: "connected", webhook_secret_set: false, last_delivery_at: null, used: true }] }));
    const first = renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={waiting} paths={[waiting, ...OTHERS]} connected={connectedProviders([waiting])} onClose={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-path-source-row").getAttribute("data-state")).toBe("waiting");
    expect(screen.getByTestId("ctem-path-source-state").textContent).toBe("Waiting for webhook");
    first.unmount();
    const fresh = codePath(GH({ integrations: [{ id: "i-gh-1", name: "GitHub Filigran", status: "connected", webhook_secret_set: true, last_delivery_at: null, used: true }] }));
    const second = renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={fresh} paths={[fresh, ...OTHERS]} connected={connectedProviders([fresh])} onClose={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-path-source-state").textContent).toBe("On, no delivery yet");
    second.unmount();
    const allOff = codePath(GH({ integrations: [{ id: "i-gh-1", name: "GitHub Filigran", status: "connected", webhook_secret_set: true, last_delivery_at: null, used: false }] }));
    renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={allOff} paths={[allOff, ...OTHERS]} connected={connectedProviders([allOff])} onClose={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-path-source-row").getAttribute("data-state")).toBe("off");
  });

  it("never reads a path Enabled when its webhook listeners wait for a webhook or are off (UX r1 #2796 B1)", () => {
    const conn = (secret: boolean, used = true) => ({ id: `i-${secret}-${used}`, name: "GitHub", status: "connected", webhook_secret_set: secret, last_delivery_at: null, used });
    const aikido = (integrations: ReturnType<typeof conn>[]) => ({ ...GH({ integrations }), provider: "aikido", key: "aikido:listener" });
    // The API's Enabled is demoted: waiting + off is Not set up, off alone is Off, one live listener beside them is partial.
    expect(pathStatus({ status: "enabled", sources: [GH({ integrations: [conn(false)] }), aikido([conn(true, false)])] })).toBe("not_set_up");
    expect(pathStatus({ status: "enabled", sources: [aikido([conn(true, false)])] })).toBe("off");
    expect(pathStatus({ status: "enabled", sources: [GH({ integrations: [conn(true)] }), aikido([conn(false)])] })).toBe("partly_enabled");
    expect(pathStatus({ status: "partly_enabled", sources: [GH({ integrations: [conn(false)] })] })).toBe("not_set_up");
    // Derived without an API status the same way; a listener that can deliver keeps Enabled.
    expect(pathStatus({ sources: [GH({ integrations: [conn(false)] })] })).toBe("not_set_up");
    expect(pathStatus({ status: "enabled", sources: [GH({ integrations: [conn(true)] })] })).toBe("enabled");
    // An older API without the integrations list keeps its status.
    expect(pathStatus({ status: "enabled", sources: [GH({ integrations: undefined })] })).toBe("enabled");
  });

  it("lays every integration row on one shared grid, a disconnected row included (UX r1 #2796 B2)", () => {
    const path = codePath(GH({ integrations: [...GH().integrations, { id: "i-gh-3", name: "GitHub archive", status: "disconnected", webhook_secret_set: false, last_delivery_at: null, used: true }] }));
    open(path);
    const list = within(configOf("github")).getByTestId("ctem-path-source-integration-rows");
    // Every row, the disconnected one without a switch included, fills the same four cells of the shared grid.
    const rows = within(list).getAllByTestId("ctem-path-source-integration-row");
    expect(rows).toHaveLength(GH().integrations.length + 1);
    for (const row of rows) {
      expect(row.children).toHaveLength(4);
    }
  });
});
