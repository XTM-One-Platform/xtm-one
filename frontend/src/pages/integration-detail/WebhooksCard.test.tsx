// The Webhooks card shows the integration webhook URL only for events that can
// arrive by webhook: a provider whose events are all polled has nothing to send
// to it, so it gets no "Webhook URL" block (and no card when nothing else shows).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, ProviderCatalogItem } from "@/types";
import { WebhooksCard } from "./WebhooksCard";

let notionToken: string | null = null;
vi.mock("@/hooks/api/useIntegrationApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useIntegrationApi")>()),
  useNotionWebhookSetup: () => ({
    data: {
      webhook_url: "https://one.example/api/hooks/integrations/i-1",
      verification_token: notionToken,
      events: [],
      next_step: "",
    },
    isLoading: false,
    isError: false,
  }),
}));

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

describe("WebhooksCard Notion setup block", () => {
  const notion = catalog("Notion", ["webhook"]);

  it("shows a manager the URL and the verification token Notion sent", () => {
    notionToken = "secret_abc";
    renderWithProviders(<WebhooksCard integration={integration("notion")} catalogEntry={notion} baseUrl="https://one.example" canManage />);
    expect(screen.getByTestId("notion-webhook-setup")).toBeTruthy();
    expect(screen.getByText("https://one.example/api/hooks/integrations/i-1")).toBeTruthy();
    expect(screen.getByText("secret_abc")).toBeTruthy();
    expect(screen.queryByText("Set content type to", { exact: false })).toBeNull();
  });

  it("tells a manager the token arrives after the subscription is created", () => {
    notionToken = null;
    renderWithProviders(<WebhooksCard integration={integration("notion")} catalogEntry={notion} baseUrl="https://one.example" canManage />);
    expect(screen.getByText("No verification token received yet", { exact: false })).toBeTruthy();
  });

  it("hides the setup block and the token from a non-manager", () => {
    notionToken = "secret_abc";
    renderWithProviders(<WebhooksCard integration={integration("notion")} catalogEntry={notion} baseUrl="https://one.example" />);
    expect(screen.queryByTestId("notion-webhook-setup")).toBeNull();
    expect(screen.queryByText("secret_abc")).toBeNull();
  });
});
