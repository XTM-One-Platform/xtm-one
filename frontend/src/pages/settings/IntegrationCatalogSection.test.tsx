// Settings > Integration catalog > a registration-only provider: each
// registered platform's address, which the platform itself sent, is a link
// only when it is a web address (`lib/safe-url.ts`), else plain text.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { PlatformRegistration } from "@/hooks/api/usePlatformApi";
import type { ProviderCatalogItem } from "@/types";
import { IntegrationCatalogSection } from "./IntegrationCatalogSection";

const opencti = {
  provider: "opencti",
  name: "OpenCTI",
  category: "integration",
  integration_category: "threat_intel",
  auth_type: "platform_managed",
  fields: [],
  extra_fields: null,
  extra_field_options: null,
  description: "Threat intelligence platform",
  logo: null,
  integration_types: [],
  available_events: [],
  available_tools: [],
  element_types: [],
  is_registration_only: true,
  disabled: false,
} as unknown as ProviderCatalogItem;

let registrations: PlatformRegistration[] = [];
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useProviderCatalog: () => ({ data: [opencti] }),
  useImportIntegrationBundle: () => idle,
  useSetCustomIntegrationProviderEnabled: () => idle,
  useUninstallCustomIntegrationProvider: () => idle,
}));
vi.mock("@/hooks/api/useSettingsApi", () => ({ useSetProviderDisabled: () => idle }));
vi.mock("@/hooks/api/usePlatformApi", () => ({
  usePlatformRegistrations: () => ({ data: registrations }),
  usePurgeRegistration: () => idle,
}));

function openRegistration(platformUrl: string): HTMLElement {
  registrations = [
    {
      id: "reg-1",
      platform_identifier: "opencti",
      platform_url: platformUrl,
      platform_title: "Production OpenCTI",
      platform_version: "6.9.0",
      status: "active",
      registered_at: "2026-10-01T00:00:00Z",
      last_heartbeat_at: "2026-10-13T00:00:00Z",
    } as unknown as PlatformRegistration,
  ];
  renderWithProviders(<IntegrationCatalogSection />);
  fireEvent.click(screen.getAllByText("OpenCTI")[0]);
  // The registration's own card in the provider dialog.
  return screen.getByText("Production OpenCTI").closest(".rounded-sm") as HTMLElement;
}

const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("IntegrationCatalogSection registered platform URL", () => {
  it("links a web address", () => {
    const card = openRegistration("https://opencti.example.com");
    expect(hrefs(card)).toEqual(["https://opencti.example.com/"]);
  });

  it.each(HOSTILE_URLS)("shows %s as text, never as a link", (_label, url) => {
    const card = openRegistration(url);
    expect(hrefs(card)).toEqual([]);
    expect(card.textContent).toContain(url);
  });
});
