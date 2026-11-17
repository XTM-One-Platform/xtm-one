// The "provisioned in OpenCTI / OpenAEV" line of an integration-event
// assignment (CTEM fleets S2e, #8991): one state per server status, the
// trigger count, the last check and the last event, in en and fr.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { TriggerConfig } from "@/types";
import { ProvisioningSummary } from "./ProvisioningSummary";
import { provisioningView } from "./provisioning-state";

const minutesAgo = (n: number) => new Date(Date.now() - n * 60_000).toISOString();

const opencti = (extra: Partial<TriggerConfig> = {}): TriggerConfig => ({
  provider: "opencti",
  event: "create",
  provision: { fleet: "Threat response", purpose: "New intrusion sets", entity_types: ["Intrusion-Set"] },
  ...extra,
});

describe("provisioningView", () => {
  it("is null for a provider XTM One provisions nothing in", () => {
    expect(provisioningView({ provider: "github", event: "push" }, true)).toBeNull();
  });

  it("is null for an OpenCTI trigger that declares nothing and holds nothing", () => {
    expect(provisioningView({ provider: "opencti", event: "create" }, true)).toBeNull();
  });

  it("reads a declared spec as pending when enabled and as disabled when paused", () => {
    expect(provisioningView(opencti(), true)?.kind).toBe("pending");
    expect(provisioningView(opencti(), false)?.kind).toBe("disabled");
  });

  it("counts one trigger per OpenAEV event", () => {
    const view = provisioningView(
      {
        provider: "openaev",
        provision: { fleet: "Exposure validation", purpose: "Results" },
        openaev_provisioning: { status: "provisioned", name: "XTM One - Exposure validation - Results", notifier_id: "n1", trigger_ids: { simulation_finished: "t1", finding_created: "t2" } },
      },
      true,
    );
    expect(view?.kind).toBe("provisioned");
    expect(view?.triggerCount).toBe(2);
  });

  it("keeps showing an orphaned state after the spec is dropped", () => {
    const view = provisioningView({ provider: "opencti", opencti_provisioning: { status: "orphaned", trigger_id: "t1" } }, false);
    expect(view?.kind).toBe("orphaned");
  });
});

describe("ProvisioningSummary", () => {
  it("names the objects, the last check and the last event once provisioned", () => {
    renderWithProviders(
      <ProvisioningSummary
        config={opencti({ opencti_provisioning: { status: "provisioned", name: "XTM One - Threat response - New intrusion sets", trigger_id: "t1", notifier_id: "n1", checked_at: minutesAgo(5) } })}
        enabled
        lastTriggeredAt={minutesAgo(120)}
      />,
    );
    expect(screen.getByText("Provisioned in OpenCTI")).toBeTruthy();
    expect(screen.getByText("One trigger and its notifier, named XTM One - Threat response - New intrusion sets.")).toBeTruthy();
    expect(screen.getByText("Checked 5 minutes ago · Last event 2 hours ago")).toBeTruthy();
  });

  it("says no event came yet on a provisioned trigger that never ran", () => {
    renderWithProviders(
      <ProvisioningSummary config={opencti({ opencti_provisioning: { status: "provisioned", name: "X", trigger_id: "t1", checked_at: minutesAgo(0) } })} enabled lastTriggeredAt={null} />,
    );
    expect(screen.getByText("Checked just now · No event yet")).toBeTruthy();
  });

  it("never shows the raw server error, only the catalogued sentence", () => {
    renderWithProviders(
      <ProvisioningSummary config={opencti({ opencti_provisioning: { status: "error", error: "labels not found in OpenCTI: ['ai']", checked_at: minutesAgo(1) } })} enabled lastTriggeredAt={null} />,
    );
    expect(screen.getByText("Provisioning in OpenCTI failed")).toBeTruthy();
    expect(screen.queryByText(/labels not found/)).toBeNull();
  });

  it("tells a paused assignment that nothing is created until an administrator enables it", () => {
    renderWithProviders(<ProvisioningSummary config={opencti()} enabled={false} lastTriggeredAt={null} />);
    expect(screen.getByText("Not provisioned in OpenCTI while paused")).toBeTruthy();
    expect(screen.getByText("Nothing is created in OpenCTI until an administrator enables this assignment.")).toBeTruthy();
  });

  it("renders nothing for an assignment without provisioning", () => {
    renderWithProviders(<ProvisioningSummary config={{ provider: "opencti", event: "create" }} enabled lastTriggeredAt={null} />);
    expect(screen.queryByTestId("provisioning-summary")).toBeNull();
  });

  it("reads in French with localized relative times", () => {
    renderWithProviders(
      <ProvisioningSummary
        config={{ provider: "openaev", provision: {}, openaev_provisioning: { status: "provisioned", name: "XTM One - E - R", trigger_ids: { a: "1", b: "2" }, checked_at: minutesAgo(5) } }}
        enabled
        lastTriggeredAt={null}
      />,
      { locale: "fr" },
    );
    expect(screen.getByText("Provisionné dans OpenAEV")).toBeTruthy();
    expect(screen.getByText("2 déclencheurs et leur notificateur, nommés XTM One - E - R.")).toBeTruthy();
    const line = screen.getByText(/Vérifié/).textContent ?? "";
    expect(line).toContain("il y a 5 minutes");
    expect(line).not.toContain("ago");
  });
});
