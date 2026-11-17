// The OpenCRQ version card on an integration's Overview: what it says of
// each state of the instance's manifest, the tools and fields it lists, and that
// it stays out of the way while it loads or when the route fails.

import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Integration, OpenCrqCompatibility } from "@/types";
import { visibleProviderOverviewCards } from "../provider-tabs";
import { OpenCrqVersionCard } from "./OpenCrqVersionCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const integration = { id: "i-1", provider: "opencrq", name: "OpenCRQ" } as Integration;

const compatibility = (overrides: Partial<OpenCrqCompatibility> = {}): OpenCrqCompatibility => ({
  reported_version: "1.4.0",
  manifest_source: "registration",
  manifest_state: "current",
  fingerprint: "abababababab",
  received_at: "2026-10-22T08:30:00Z",
  tool_count: 200,
  hidden_tools: [],
  limited_tools: [],
  ...overrides,
});

const answer = (data: OpenCrqCompatibility) => vi.mocked(api.get).mockResolvedValue(data);

describe("OpenCrqVersionCard", () => {
  it("asks the compatibility route of its integration", async () => {
    answer(compatibility());
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    await screen.findByText("OpenCRQ version");
    expect(api.get).toHaveBeenCalledWith("/integrations/i-1/opencrq/compatibility");
  });

  it("shows the version, the short fingerprint and when the manifest was received", async () => {
    answer(compatibility());
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText("Manifest up to date")).toBeTruthy();
    expect(screen.getByText("Version 1.4.0")).toBeTruthy();
    expect(screen.getByText("This OpenCRQ described its tools to XTM One. The agents get only what it can serve.")).toBeTruthy();
    expect(screen.getByText("Fingerprint abababababab")).toBeTruthy();
    expect(screen.getByText(/Received .*2026/)).toBeTruthy();
  });

  it("names a local build, which reports an empty version", async () => {
    answer(compatibility({ reported_version: "" }));
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText("Development build")).toBeTruthy();
  });

  it.each([
    ["pending", "abababababab", "Manifest pending", /announced a new description of its tools that XTM One has not received yet/],
    ["unavailable", null, "Manifest unavailable", /does not describe its tools to XTM One/],
    ["not_accepted_default_token", null, "Manifest not accepted", /Set a registration token of your own\./],
  ] as const)("warns when the manifest is %s", async (manifest_state, fingerprint, label, sentence) => {
    answer(compatibility({ manifest_state, fingerprint, received_at: null }));
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText(label)).toBeTruthy();
    expect(screen.getByText(sentence)).toBeTruthy();
    expect(screen.queryByText(/Received/)).toBeNull();
  });

  it("says when the manifest is read from the instance at the first call", async () => {
    answer(compatibility({ reported_version: null, manifest_source: "none", manifest_state: null, fingerprint: null, received_at: null }));
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText("Read at the first call")).toBeTruthy();
    expect(screen.getByText(/at the first OpenCRQ call of each conversation or run\./)).toBeTruthy();
    expect(screen.queryByText(/^Version /)).toBeNull();
    expect(screen.queryByText(/Fingerprint/)).toBeNull();
  });

  it("lists the hidden tools with the procedures they are missing", async () => {
    answer(
      compatibility({
        hidden_tools: [
          { name: "create_opencrq_group", missing_procedures: ["groups.createGroup"] },
          { name: "delete_opencrq_group", missing_procedures: ["groups.deleteGroup", "groups.listGroups"] },
        ],
      }),
    );
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText("2 tools are hidden from the agents: this instance does not support them.")).toBeTruthy();
    expect(screen.getByText("create_opencrq_group")).toBeTruthy();
    expect(screen.getByText("Missing: groups.createGroup")).toBeTruthy();
    expect(screen.getByText("delete_opencrq_group")).toBeTruthy();
    expect(screen.getByText("Missing: groups.deleteGroup, groups.listGroups")).toBeTruthy();
  });

  it("lists the fields a served tool would lose, and the ones it never sends", async () => {
    answer(
      compatibility({
        limited_tools: [
          {
            name: "update_opencrq_risk_scenario",
            fields: [
              { procedure: "riskScenarios.updateRiskScenario", path: "ownerId", kind: "unknown" },
              { procedure: "riskScenarios.updateRiskScenario", path: "slug", kind: "missing" },
            ],
          },
        ],
      }),
    );
    renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(await screen.findByText("1 tool has fields this instance does not accept. A call that sends one is refused.")).toBeTruthy();
    expect(screen.getByText("update_opencrq_risk_scenario")).toBeTruthy();
    expect(screen.getByText("ownerId")).toBeTruthy();
    expect(screen.getByText("slug (required)")).toBeTruthy();
  });

  it("renders nothing while the route answers", () => {
    vi.mocked(api.get).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    expect(container.textContent).toBe("");
  });

  it("renders nothing when the route fails", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("boom"));
    const { container } = renderWithProviders(<OpenCrqVersionCard integration={integration} />);

    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});

describe("the OpenCRQ Overview card", () => {
  it("is shown to everyone who can open the integration", () => {
    const viewers = [
      { userId: undefined, isAdmin: false },
      { userId: "someone-else", isAdmin: false },
      { userId: "admin", isAdmin: true },
    ];
    for (const viewer of viewers) {
      expect(visibleProviderOverviewCards(integration, viewer).map((card) => card.key)).toEqual(["version"]);
    }
  });

  it("is not added to another provider's Overview", () => {
    expect(visibleProviderOverviewCards({ ...integration, provider: "opencti" }, { userId: "u", isAdmin: true })).toEqual([]);
  });
});
