// Which event mechanisms make the integration webhook URL relevant. The create
// form and the Configuration tab both read these: a provider whose events are
// all polled (Pennylane, OpenCRQ) shows the polling line, never a webhook URL
// nobody configures (#1009).

import { describe, expect, it } from "vitest";

import type { ProviderCatalogItem } from "@/types";
import { hasOnlyPolledEvents, hasWebhookEvents } from "./webhooks-card-blocks";

const catalog = (mechanisms: string[]) =>
  ({
    available_events: mechanisms.map((mechanism, index) => ({
      event: `event_${index}`,
      label: `Event ${index}`,
      description: "",
      mechanism,
    })),
  }) as unknown as ProviderCatalogItem;

describe("hasWebhookEvents / hasOnlyPolledEvents", () => {
  it("a polling-only provider has no webhook events and shows the polling line", () => {
    const entry = catalog(["polling", "polling", "polling"]);
    expect(hasWebhookEvents(entry)).toBe(false);
    expect(hasOnlyPolledEvents(entry)).toBe(true);
  });

  it.each([
    ["webhook", ["webhook"]],
    ["hybrid", ["hybrid"]],
    ["mixed polling and webhook", ["polling", "webhook"]],
  ])("a provider with %s events keeps the webhook URL", (_label, mechanisms) => {
    const entry = catalog(mechanisms);
    expect(hasWebhookEvents(entry)).toBe(true);
    expect(hasOnlyPolledEvents(entry)).toBe(false);
  });

  it("a provider with no events shows neither", () => {
    expect(hasWebhookEvents(catalog([]))).toBe(false);
    expect(hasOnlyPolledEvents(catalog([]))).toBe(false);
    expect(hasWebhookEvents(undefined)).toBe(false);
    expect(hasOnlyPolledEvents(undefined)).toBe(false);
  });
});
