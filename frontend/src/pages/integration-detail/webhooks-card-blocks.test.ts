// The integration webhook URL is shown (on the New integration form and on the
// Webhooks card) only when an event of the provider can arrive by webhook: a
// provider whose events are all polled, such as NetSuite, never calls it.

import { describe, expect, it } from "vitest";

import type { ProviderCatalogItem } from "@/types";
import { hasWebhookDeliveredEvents } from "./webhooks-card-blocks";

const catalog = (mechanisms: string[]) =>
  ({
    name: "Provider",
    available_events: mechanisms.map((mechanism, index) => ({
      event: `event_${index}`,
      label: `Event ${index}`,
      description: "",
      mechanism,
    })),
  }) as unknown as ProviderCatalogItem;

describe("hasWebhookDeliveredEvents", () => {
  it("is false for a polling-only provider", () => {
    expect(hasWebhookDeliveredEvents(catalog(["polling", "polling", "polling", "polling", "polling"]))).toBe(false);
  });

  it("is false for a provider with no event or no catalog entry", () => {
    expect(hasWebhookDeliveredEvents(catalog([]))).toBe(false);
    expect(hasWebhookDeliveredEvents(undefined)).toBe(false);
  });

  it.each([
    ["webhook", ["webhook"]],
    ["hybrid", ["hybrid"]],
    ["mixed polling and webhook", ["polling", "webhook"]],
  ])("is true for %s events", (_label, mechanisms) => {
    expect(hasWebhookDeliveredEvents(catalog(mechanisms))).toBe(true);
  });
});
