import { describe, it, expect } from "vitest";
import type { PlatformRegistration } from "@/hooks/api/usePlatformApi";
import { productSwitcherEntries } from "./product-switcher";

/**
 * The product switcher lists the suite as OpenCTI and OpenAEV list theirs:
 * every product, connected or not, so the menu is never empty and always says
 * where each product is.
 */

// The backend's XTM_HUB_DEFAULT_URL, as the registration status serves it.
const DEFAULT_HUB = "https://hub.default.example.test";

const PUBLIC_PAGES = { opencti: "https://filigran.io/platform/opencti/", openaev: "https://filigran.io/platform/openaev/" };

const registration = (overrides: Partial<PlatformRegistration>): PlatformRegistration => ({
  id: "r",
  platform_identifier: "opencti",
  platform_id: null,
  platform_url: "https://cti.example.test",
  platform_title: "OpenCTI",
  platform_version: "6.0.0",
  contract: "EE",
  license_type: null,
  license_validated: true,
  integration_id: null,
  status: "active",
  registered_at: "2026-10-01T00:00:00Z",
  last_heartbeat_at: "2026-10-19T00:00:00Z",
  healthy: true,
  ...overrides,
} as PlatformRegistration);

const last = <T,>(items: T[]): T | undefined => items[items.length - 1];

const entries = (overrides: Partial<Parameters<typeof productSwitcherEntries>[0]> = {}) =>
  productSwitcherEntries({
    registrations: [],
    hubRegistration: { status: "unregistered", hub_url: null, default_hub_url: DEFAULT_HUB },
    publicPages: PUBLIC_PAGES,
    hubRegistrationPath: null,
    ...overrides,
  });

describe("productSwitcherEntries", () => {
  it("lists every product with a public page when nothing is connected", () => {
    expect(entries().map(({ product, href, target }) => ({ product, href, target }))).toEqual([
      { product: "opencti", href: PUBLIC_PAGES.opencti, target: "public" },
      { product: "openaev", href: PUBLIC_PAGES.openaev, target: "public" },
      { product: "xtm_hub", href: DEFAULT_HUB, target: "public" },
    ]);
  });

  it("opens a connected platform at its own address, named by its title", () => {
    const [opencti] = entries({
      registrations: [registration({ id: "r1", platform_title: "OpenCTI production", healthy: false })],
    });
    expect(opencti).toEqual({
      id: "r1", product: "opencti", label: "OpenCTI production", href: "https://cti.example.test/", target: "stale",
    });
  });

  it("reads the pre-rename identifiers as the products they became", () => {
    const products = entries({
      registrations: [
        registration({ id: "a", platform_identifier: "openbas", platform_url: "https://aev.example.test" }),
        registration({ id: "c", platform_identifier: "opengrc", platform_url: "https://crq.example.test" }),
      ],
    }).map(({ product, href }) => [product, href]);
    expect(products).toContainEqual(["openaev", "https://aev.example.test/"]);
    expect(products).toContainEqual(["opencrq", "https://crq.example.test/"]);
  });

  it("lists OpenCRQ, which has no public page yet, only once it registered", () => {
    expect(entries().map((entry) => entry.product)).not.toContain("opencrq");
  });

  it.each(["javascript:alert(1)", "data:text/html,x", "/admin", "//evil.example.test", ""])(
    "never links a registration whose address is %j, and falls back to the public page",
    (platformUrl) => {
      const [opencti] = entries({ registrations: [registration({ platform_url: platformUrl })] });
      expect(opencti).toMatchObject({ product: "opencti", href: PUBLIC_PAGES.opencti, target: "public" });
    },
  );

  it("leaves out a registration of a product the switcher does not know", () => {
    const listed = entries({ registrations: [registration({ platform_identifier: "some-other-product" })] });
    expect(listed.map((entry) => entry.product)).toEqual(["opencti", "openaev", "xtm_hub"]);
  });

  it("opens the XTM Hub this platform is registered with", () => {
    const hub = last(entries({
      hubRegistration: { status: "registered", hub_url: "https://hub.example.test/", default_hub_url: DEFAULT_HUB },
    }));
    expect(hub).toMatchObject({ product: "xtm_hub", href: "https://hub.example.test", target: "connected" });
  });

  it("sends whoever may register the platform to the registration, everyone else to the XTM Hub", () => {
    expect(last(entries({ hubRegistrationPath: "/settings/platform" }))).toMatchObject({
      to: "/settings/platform", target: "register",
    });
    expect(last(entries())).toMatchObject({ href: DEFAULT_HUB, target: "public" });
  });

  it("lists the XTM Hub for everyone else only once the default XTM Hub is known", () => {
    expect(entries({ hubRegistration: undefined }).map((entry) => entry.product)).not.toContain("xtm_hub");
  });
});
