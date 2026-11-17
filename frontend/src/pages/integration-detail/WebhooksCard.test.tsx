// The Webhooks card shows the integration webhook URL only for events that can
// arrive by webhook: a provider whose events are all polled has nothing to send
// to it, so it gets no "Webhook URL" block (and no card when nothing else shows).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, ProviderCatalogItem } from "@/types";
import { WebhooksCard } from "./WebhooksCard";

const integration = (provider: string) =>
  ({ id: "i-1", provider, name: provider, status: "connected", auth_type: "api_key", extra_config: {} }) as unknown as Integration;

const catalog = (name: string, mechanisms: string[]) =>
  ({
    name,
    available_events: mechanisms.map((mechanism, index) => ({
      event: `event_${index}`,
      label: `Event ${index}`,
      description: "",
      mechanism,
    })),
  }) as unknown as ProviderCatalogItem;

describe("WebhooksCard webhook URL block", () => {
  it("renders no Webhook URL block and no card for a provider whose events are all polled", () => {
    renderWithProviders(
      <WebhooksCard integration={integration("opencrq")} catalogEntry={catalog("OpenCRQ", ["polling", "polling"])} baseUrl="https://one.example" />,
    );
    expect(screen.queryByText("OpenCRQ Webhook URL")).toBeNull();
    expect(screen.queryByText("https://one.example/api/hooks/integrations/i-1")).toBeNull();
    expect(screen.queryByText("Webhooks")).toBeNull();
  });

  it.each([
    ["webhook", ["webhook"]],
    ["hybrid", ["hybrid"]],
    ["mixed polling and webhook", ["polling", "webhook"]],
  ])("keeps the Webhook URL block for %s events", (_label, mechanisms) => {
    renderWithProviders(
      <WebhooksCard integration={integration("docusign")} catalogEntry={catalog("DocuSign", mechanisms)} baseUrl="https://one.example" />,
    );
    expect(screen.getByText("DocuSign Webhook URL")).toBeTruthy();
    expect(screen.getByText("https://one.example/api/hooks/integrations/i-1")).toBeTruthy();
  });
});
