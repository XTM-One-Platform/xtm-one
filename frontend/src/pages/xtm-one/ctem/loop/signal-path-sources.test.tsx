// The sources of one trigger path managed from the Sources tab (#1445): exclusion rules and the side panel.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { SignalPathSourcesSheet } from "./SignalPathSourcesSheet";
import { canExclude, connectTarget, connectedProviders, pathStatus, sourceState, withExclusion } from "./signal-sources";

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

describe("the sources side panel (#1445)", () => {
  it("lists every source with its state and the control that exists for it", () => {
    open();
    expect(rowsBy().map((r) => r.getAttribute("data-state"))).toEqual(["bound", "excluded", "not_connected"]);
    const [shodan, gti, censys] = [configOf("shodan"), configOf("gti"), configOf("censys")];
    expect(within(shodan).getByTestId("ctem-path-source-runs-on").textContent).toContain("Shodan prod");
    expect(within(shodan).getByTestId("ctem-path-source-shared").textContent).toContain("Stale or orphaned asset, Attack-surface drift");
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
    const body = state.saved[0] as { fields: Record<string, { enabled: boolean; integration: string }> };
    // `enabled: false` with none declared is the legacy default read as on: the off names the integration.
    expect(body.fields.evidence_health).toMatchObject({ enabled: false, integration: "i-opencrq" });
    expect(Object.keys(body.fields)).toEqual(["evidence_health"]);
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
});
