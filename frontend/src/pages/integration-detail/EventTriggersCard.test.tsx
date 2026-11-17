// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { ProviderCatalogItem } from "@/types";
import { EventTriggersCard } from "./EventTriggersCard";

const entry = (events: ProviderCatalogItem["available_events"]) =>
  ({ provider: "github", name: "GitHub", available_events: events }) as unknown as ProviderCatalogItem;

describe("EventTriggersCard", () => {
  it("names a catalog event and its description in the viewer's language", () => {
    renderWithProviders(
      <EventTriggersCard
        catalogEntry={entry([
          {
            event: "create",
            label: "Branch or tag created",
            description: "Triggers when a branch or tag is created.",
            mechanism: "webhook",
          },
        ] as ProviderCatalogItem["available_events"])}
      />,
      { locale: "fr-fr" },
    );
    expect(screen.getByText("Branche ou tag créé")).toBeTruthy();
    expect(screen.queryByText("Branch or tag created")).toBeNull();
    expect(screen.getByText("Se déclenche lorsqu'une branche ou un tag est créé.")).toBeTruthy();
    expect(screen.queryByText("Triggers when a branch or tag is created.")).toBeNull();
  });

  it("shows a label the catalog does not hold as written", () => {
    renderWithProviders(
      <EventTriggersCard
        catalogEntry={entry([
          { event: "custom", label: "Custom thing happened", description: "Fires on {custom} input.", mechanism: "polling" },
        ] as ProviderCatalogItem["available_events"])}
      />,
      { locale: "fr-fr" },
    );
    expect(screen.getByText("Custom thing happened")).toBeTruthy();
    expect(screen.getByText("Fires on {custom} input.")).toBeTruthy();
  });
});
