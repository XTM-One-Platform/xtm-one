// The Event Triggers and Webhooks cards share a row of the Configuration tab.
// When the Webhooks card renders nothing (every event polled), Event Triggers
// takes the full row instead of leaving the right column empty.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, ProviderCatalogItem } from "@/types";
import { ConfigTab } from "./ConfigTab";
import { webhooksCardShowsBlock } from "./webhooks-card-blocks";

// The cards above the row read their own APIs; this test is about the row.
vi.mock("./ConfigurationCard", () => ({ ConfigurationCard: () => null }));
vi.mock("./EmailSignatureCard", () => ({ EmailSignatureCard: () => null }));
vi.mock("./ToolsCard", () => ({ ToolsCard: () => null }));
vi.mock("@/hooks/api/useA2AClientApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useA2AClientApi")>()),
  useA2ADirection: () => undefined,
}));

const integration = (provider: string) =>
  ({ id: "i-1", provider, name: provider, status: "connected", auth_type: "api_key", extra_config: {} }) as unknown as Integration;

const catalog = (name: string, mechanisms: string[]) =>
  ({
    provider: name.toLowerCase(),
    name,
    available_events: mechanisms.map((mechanism, index) => ({
      event: `event_${index}`,
      label: `Event ${index}`,
      description: "",
      mechanism,
    })),
  }) as unknown as ProviderCatalogItem;

const renderTab = (provider: string, entry: ProviderCatalogItem) =>
  renderWithProviders(
    <ConfigTab integration={integration(provider)} catalogEntry={entry} canManage isRegistrationOnly={false} baseUrl="https://one.example" />,
  );

describe("ConfigTab events row", () => {
  it("gives Event Triggers the full row when every event is polled", () => {
    renderTab("opencrq", catalog("OpenCRQ", ["polling", "polling"]));
    expect(screen.getByTestId("config-events-row").getAttribute("data-columns")).toBe("1");
    expect(screen.queryByText("Webhooks")).toBeNull();
  });

  it.each([
    ["webhook", ["webhook"]],
    ["hybrid", ["hybrid"]],
  ])("keeps two columns when the Webhooks card shows a block (%s events)", (_label, mechanisms) => {
    renderTab("docusign", catalog("DocuSign", mechanisms));
    expect(screen.getByTestId("config-events-row").getAttribute("data-columns")).toBe("2");
    expect(screen.getByText("Webhooks")).toBeTruthy();
  });
});

describe("webhooksCardShowsBlock", () => {
  it("agrees with what the Webhooks card renders", () => {
    expect(webhooksCardShowsBlock(integration("opencrq"), catalog("OpenCRQ", ["polling"]), true)).toBe(false);
    expect(webhooksCardShowsBlock(integration("docusign"), catalog("DocuSign", ["hybrid"]), true)).toBe(true);
    // Microsoft 365 and Slack always carry their own events block.
    expect(webhooksCardShowsBlock(integration("microsoft"), catalog("Microsoft 365", ["polling"]), false)).toBe(true);
    expect(webhooksCardShowsBlock(integration("slack"), catalog("Slack", ["polling"]), false)).toBe(true);
  });
});
