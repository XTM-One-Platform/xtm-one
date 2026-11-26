// The generic signed webhook source of a trigger path (#2506): listed in the path table, switched there,
// created off from "Add a signed webhook", its secret shown once, rotated and its mapping tested in its dialog.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemSignalPath, CtemWebhookSource } from "@/hooks/api/useCtemSignalsApi";
import { SignalPathSourcesSheet } from "./SignalPathSourcesSheet";
import { signedRequestExample, sourceDisplayState } from "./signal-sources";

const state = vi.hoisted(() => ({
  hooks: [] as CtemWebhookSource[],
  created: [] as unknown[],
  updated: [] as unknown[],
  rotated: [] as string[],
  tested: [] as unknown[],
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: true }) }));
vi.mock("@/hooks/api/useIntegrationApi", () => ({ useIntegrations: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useAuthApi", () => ({ useUsers: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { version: 1, profile: { name: "Acme" } } }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
}));
vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => {
  type Opts = { onSuccess?: (value: never) => void };
  return {
    ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
    useCtemPlatformSources: () => ({ data: { sources: [] } }),
    useCtemWebhookSources: () => ({ data: { sources: state.hooks } }),
    useCreateCtemWebhookSource: () => ({
      isPending: false,
      mutate: (body: { name: string }, opts: Opts) => {
        state.created.push(body);
        opts.onSuccess?.({ ...hook({ id: "w-new", name: body.name, enabled: false }), secret: "s3cr3t-once" } as never);
      },
    }),
    useUpdateCtemWebhookSource: () => ({ isPending: false, mutate: (body: unknown) => state.updated.push(body) }),
    useRotateCtemWebhookSource: () => ({
      isPending: false,
      mutate: (id: string, opts: Opts) => {
        state.rotated.push(id);
        opts.onSuccess?.({ ...hook({ id }), secret: "n3w-s3cr3t" } as never);
      },
    }),
    useTestCtemWebhookSource: () => ({
      isPending: false,
      mutate: (body: unknown, opts: Opts) => {
        state.tested.push(body);
        opts.onSuccess?.({ ok: true, id: "evt-1", label: "vpn.example.com", severity: "high" } as never);
      },
    }),
  };
});

function hook(over: Partial<CtemWebhookSource> = {}): CtemWebhookSource {
  return {
    id: "w-1",
    trigger_path: "new_exposure",
    name: "Scanner",
    enabled: true,
    subject_kind: "asset",
    mapping: { id: "event.id", label: "event.host" },
    receiver_path: "/api/hooks/ctem-webhook-sources/w-1",
    receiver_url: "https://one.example/api/hooks/ctem-webhook-sources/w-1",
    signature_header: "X-XTM-Signature",
    secret_rotated_at: null,
    last_delivery_at: null,
    last_error: null,
    created_at: null,
    ...over,
  };
}

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
    { provider: "webhook", kind: "webhook", key: "webhook:webhook:w-1", name: "Scanner", event: "", bound: true, webhook_id: "w-1", count_30d: 3 },
  ],
};

function open(path: CtemSignalPath = EXPOSURE) {
  return renderWithProviders(
    <MemoryRouter>
      <SignalPathSourcesSheet path={path} paths={[path]} connected={new Set()} onClose={() => undefined} />
    </MemoryRouter>,
  );
}

/** The dialog on screen: a closed one may stay mounted while it animates out. */
function lastDialog(): HTMLElement {
  const dialogs = screen.getAllByTestId("ctem-webhook-source-dialog");
  return dialogs[dialogs.length - 1];
}

beforeEach(() => {
  state.hooks = [hook()];
  state.created = [];
  state.updated = [];
  state.rotated = [];
  state.tested = [];
});

describe("signed webhook sources (#2506)", () => {
  it("reads On by its own name, never Not connected, and Off when switched off", () => {
    const source = (EXPOSURE.sources ?? [])[0];
    expect(sourceDisplayState(source, new Set())).toBe("bound");
    expect(sourceDisplayState({ ...source, bound: false }, new Set())).toBe("off");
  });

  it("lists the webhook source in the path table with its switch, and switches it off", () => {
    open();
    const row = screen.getByTestId("ctem-path-source-row");
    expect(row.getAttribute("data-kind")).toBe("webhook");
    expect(row.textContent).toContain("Scanner");
    expect(within(row).getByTestId("ctem-path-source-state").textContent).toBe("On");
    fireEvent.click(within(row).getByTestId("ctem-path-source-webhook-switch"));
    expect(state.updated).toEqual([{ id: "w-1", enabled: false }]);
  });

  it("creates a source off and shows its secret once", () => {
    open({ ...EXPOSURE, sources: [] });
    fireEvent.click(screen.getByTestId("ctem-path-source-add-webhook"));
    const dialog = lastDialog();
    fireEvent.change(within(dialog).getByTestId("ctem-webhook-source-name"), { target: { value: "Scanner" } });
    fireEvent.click(within(dialog).getByTestId("ctem-webhook-source-save"));
    expect(state.created).toEqual([
      { trigger_path: "new_exposure", name: "Scanner", mapping: { id: "event.id", label: "event.host", severity: "event.severity" } },
    ]);
    expect(within(dialog).getByTestId("ctem-webhook-source-secret").textContent).toContain("s3cr3t-once");
  });

  it("never shows a stored secret, rotates it on demand and tests the mapping without recording", () => {
    open();
    fireEvent.click(screen.getByTestId("ctem-path-source-webhook-configure"));
    const dialog = lastDialog();
    expect(within(dialog).queryByTestId("ctem-webhook-source-secret")).toBeNull();
    fireEvent.click(within(dialog).getByTestId("ctem-webhook-source-rotate"));
    expect(state.rotated).toEqual(["w-1"]);
    expect(within(dialog).getByTestId("ctem-webhook-source-secret").textContent).toContain("n3w-s3cr3t");
    fireEvent.click(within(dialog).getByTestId("ctem-webhook-source-run-test"));
    expect(state.tested).toHaveLength(1);
    expect(within(dialog).getByTestId("ctem-webhook-source-test-result").textContent).toContain("evt-1");
  });

  it("gives a signed request example that never carries the real secret", () => {
    const example = signedRequestExample("https://one.example/x", "X-XTM-Signature");
    expect(example).toContain("-hmac \"$SECRET\"");
    expect(example).toContain("X-XTM-Signature: sha256=$SIG");
  });
});
