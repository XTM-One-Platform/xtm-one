// The platform signal sources (#2434): switch, run-as, status and reason per source; Set up opens the enable dialog.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemPlatformSource, CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { SignalPathSourcesSheet } from "./SignalPathSourcesSheet";
import { connectedProviders } from "./signal-sources";
import { enableRefusalText, isProvisioned, PLATFORM_SOURCE_REASON, platformSourceReason } from "./platform-sources";

const state = vi.hoisted(() => ({
  admin: true,
  sources: [] as CtemPlatformSource[],
  enabled: [] as unknown[],
  disabled: [] as unknown[],
  refuse: null as null | { code: string; params: Record<string, string> },
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.admin }) }));
vi.mock("@/hooks/api/useIntegrationApi", () => ({ useIntegrations: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useAuthApi", () => ({
  useUsers: () => ({ data: [{ id: "u-1", email: "soc@acme.example", full_name: "SOC lead", is_active: true }] }),
  useModuleRunAsCandidates: () => ({
    data: [
      { id: "u-1", name: "SOC lead", email: "soc@acme.example", is_self: false, selectable: true, reason: null },
      { id: "u-2", name: "Intern", email: "intern@acme.example", is_self: false, selectable: false, reason: "no_integration" },
    ],
    isError: false,
  }),
}));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { version: 1, profile: { name: "Acme" } } }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
}));
vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemPlatformSources: () => ({ data: { sources: state.sources } }),
  useEnableCtemPlatformSource: () => ({
    isPending: false,
    mutate: (body: unknown, opts: { onSuccess?: () => void; onError?: (e: unknown) => void }) => {
      state.enabled.push(body);
      if (state.refuse) opts.onError?.(Object.assign(new Error("refused"), { status: 400, detail: state.refuse }));
      else opts.onSuccess?.();
    },
  }),
  useDisableCtemPlatformSource: () => ({ isPending: false, mutate: (id: string) => state.disabled.push(id) }),
}));

function source(over: Partial<CtemPlatformSource> = {}): CtemPlatformSource {
  return {
    id: null,
    provider: "openaev",
    kind: "listener",
    integration_id: "i-oaev",
    integration_name: "OpenAEV test",
    integration_connected: true,
    enabled: false,
    run_as_user_id: null,
    status: "off",
    reason: null,
    reason_code: null,
    reason_params: {},
    checked_at: null,
    ...over,
  };
}

const PROVEN: CtemSignalPath = {
  id: "proven_exploitable",
  label: "Proven exploitable",
  default_lens: "inside",
  listener: true,
  counts: {},
  total: 0,
  configured: false,
  status: "not_connected",
  sources: [{ provider: "openaev", kind: "listener", event: "A simulation proves an exposure", bound: false }],
};

/** #2506: the panel is a table; each row opens its configuration from its Configure button. */
function openConfigs() {
  for (const button of screen.queryAllByTestId("ctem-path-source-configure")) fireEvent.click(button);
}

function open() {
  const view = renderWithProviders(
    <MemoryRouter>
      <SignalPathSourcesSheet path={PROVEN} paths={[PROVEN]} connected={new Set(["openaev", ...connectedProviders([PROVEN])])} onClose={() => undefined} />
    </MemoryRouter>,
  );
  openConfigs();
  return view;
}

beforeEach(() => {
  state.admin = true;
  state.sources = [source()];
  state.enabled = [];
  state.disabled = [];
  state.refuse = null;
});

describe("platform source rules (#2434)", () => {
  it("knows which sources XTM One provisions", () => {
    expect(isProvisioned({ provider: "openaev", kind: "listener" })).toBe(true);
    expect(isProvisioned({ provider: "opencti", kind: "pir_playbook" })).toBe(true);
    expect(isProvisioned({ provider: "aikido", kind: "listener" })).toBe(false);
  });

  it("renders every reason code in words, never the English line", () => {
    const t = (key: string, values?: Record<string, string | number>) => key.replace(/\{(\w+)\}/g, (_, k) => String(values?.[k] ?? ""));
    expect(platformSourceReason({ reason_code: "run_as_no_integration", reason_params: { provider: "OpenCTI" } }, t)).toContain("no connected OpenCTI integration");
    expect(platformSourceReason({ reason_code: null, reason_params: {} }, t)).toBeNull();
    expect(enableRefusalText({ detail: { code: "no_webhook_secret", params: {} } }, t)).toBe(PLATFORM_SOURCE_REASON.no_webhook_secret);
    expect(enableRefusalText({ detail: "plain" }, t)).toBeNull();
  });
});

describe("the path panel (#2434)", () => {
  it("shows Off per integration, and Set up opens the enable dialog that provisions as the run-as", () => {
    open();
    const line = screen.getByTestId("ctem-platform-source");
    expect(within(line).getByTestId("ctem-platform-source-status").textContent).toBe("Off");
    fireEvent.click(screen.getByTestId("ctem-path-source-set-up"));
    const dialog = screen.getByTestId("ctem-platform-source-enable");
    const submit = within(dialog).getByTestId("ctem-platform-source-enable-submit");
    expect(submit).toBeDisabled();
  });

  it("a refused enable shows the reason on the run-as field, marked invalid", () => {
    state.sources = [source({ run_as_user_id: "u-1" })];
    state.refuse = { code: "run_as_no_integration", params: { provider: "OpenAEV" } };
    open();
    fireEvent.click(screen.getByTestId("ctem-path-source-set-up"));
    fireEvent.click(screen.getByTestId("ctem-platform-source-enable-submit"));
    expect(state.enabled).toEqual([{ provider: "openaev", kind: "listener", integration_id: "i-oaev", run_as_user_id: "u-1" }]);
    expect(screen.getByTestId("ctem-platform-source-refusal").textContent).toContain("no connected OpenAEV integration");
    expect(screen.getByLabelText("Act in OpenAEV as").getAttribute("aria-invalid")).toBe("true");
  });

  it("marks Run as required and explains it in an info bubble (UX round 1 on #2442, B2)", () => {
    open();
    fireEvent.click(screen.getByTestId("ctem-path-source-set-up"));
    const dialog = screen.getByTestId("ctem-platform-source-enable");
    expect(within(dialog).getByLabelText("Required").textContent).toBe("*");
    expect(within(dialog).getByLabelText("About this field")).toBeTruthy();
  });

  it("a source in error reads Error on its row and the path says how many sources are in error (B1)", () => {
    const errored: CtemSignalPath = {
      ...PROVEN,
      status: "error",
      sources: [{ provider: "openaev", kind: "listener", event: "A simulation proves an exposure", bound: false, error_count: 1 }],
    };
    state.sources = [source({ status: "error", reason_code: "provider_error", reason_params: { detail: "unreachable" } })];
    renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={errored} paths={[errored]} connected={new Set(["openaev"])} onClose={() => undefined} />
      </MemoryRouter>,
    );
    openConfigs();
    expect(screen.getByTestId("ctem-path-sources-status").textContent).toBe("1 source in error");
    expect(screen.getByTestId("ctem-path-source-state").textContent).toBe("Error");
    expect(screen.getByTestId("ctem-platform-source-status").textContent).toBe("Error");
  });

  it("a connected provider with its source off reads Not set up on the path, never Not connected (B1)", () => {
    const off: CtemSignalPath = {
      ...PROVEN,
      status: "not_set_up",
      sources: [{ provider: "openaev", kind: "listener", event: "A simulation proves an exposure", bound: false, not_set_up: true }],
    };
    renderWithProviders(
      <MemoryRouter>
        <SignalPathSourcesSheet path={off} paths={[off]} connected={new Set(["openaev"])} onClose={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-path-sources-status").textContent).toBe("Not set up");
    // One word per source, the path list's (A4 of #2436): a provisioned source with every line off reads Not set up.
    expect(screen.getByTestId("ctem-path-source-state").textContent).toBe("Not set up");
  });

  it("an enabled source shows its run-as and status; the switch off disables it", () => {
    state.sources = [source({ id: "s-1", enabled: true, run_as_user_id: "u-1", status: "connected" })];
    open();
    expect(screen.getByTestId("ctem-platform-source-run-as").textContent).toContain("SOC lead");
    expect(screen.getByTestId("ctem-platform-source-status").textContent).toBe("Connected");
    expect(screen.queryByTestId("ctem-path-source-set-up")).toBeNull();
    fireEvent.click(screen.getByTestId("ctem-platform-source-switch"));
    expect(state.disabled).toEqual(["s-1"]);
  });

  it("asks who XTM One acts as in the module, and an enabled source can change it (#2650)", () => {
    state.sources = [source({ id: "s-1", enabled: true, run_as_user_id: "u-1", status: "connected" })];
    open();
    fireEvent.click(screen.getByTestId("ctem-platform-source-run-as-change"));
    const dialog = screen.getByTestId("ctem-platform-source-enable");
    expect(within(dialog).getAllByText("Act in OpenAEV as").length).toBeGreaterThan(0);
    fireEvent.click(within(dialog).getByTestId("ctem-platform-source-enable-submit"));
    expect(state.enabled).toEqual([{ provider: "openaev", kind: "listener", integration_id: "i-oaev", run_as_user_id: "u-1" }]);
  });

  it("an error reads its reason in words", () => {
    state.sources = [source({ id: "s-1", enabled: true, run_as_user_id: "u-1", status: "error", reason_code: "run_as_inactive", reason: "x" })];
    open();
    expect(screen.getByTestId("ctem-platform-source-status").textContent).toBe("Error");
    expect(screen.getByTestId("ctem-platform-source-reason").textContent).toBe(PLATFORM_SOURCE_REASON.run_as_inactive);
  });

  it("a reader sees the states without controls", () => {
    state.admin = false;
    state.sources = [];
    open();
    expect(screen.queryByTestId("ctem-path-source-set-up")).toBeNull();
    expect(screen.queryByTestId("ctem-platform-source-switch")).toBeNull();
  });
});
