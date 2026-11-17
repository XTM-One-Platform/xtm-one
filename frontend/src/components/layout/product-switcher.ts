// SPDX-License-Identifier: Apache-2.0

/**
 * The sibling products the header's product switcher lists.
 *
 * As in OpenCTI and OpenAEV, the switcher names the suite whether or not this
 * platform is connected to it: a product it is connected to opens that
 * platform, one it is not opens its public page, so the menu is never empty.
 * A product with no public page (OpenCRQ) is listed once one registers.
 */
import type { PlatformRegistration } from "@/hooks/api/usePlatformApi";
import { safeHttpUrl } from "@/lib/safe-url";
import { xtmHubPill, type XtmHubRegistrationLike } from "@/lib/xtm-hub";

export type SuiteProduct = "opencti" | "openaev" | "opencrq" | "xtm_hub";

/** What the row leads to, which its tooltip says. */
type SwitcherTarget = "connected" | "stale" | "public" | "register";

export interface SwitcherEntry {
  id: string;
  product: SuiteProduct;
  /** The row's accessible name: the platform's own title once it registered. */
  label: string;
  href?: string;
  to?: string;
  target: SwitcherTarget;
}

export const SUITE_PRODUCT_NAMES: Record<SuiteProduct, string> = {
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
  xtm_hub: "XTM Hub",
};

/** The identifier a platform registers under, the pre-rename ones included. */
const PRODUCT_OF_IDENTIFIER: Record<string, SuiteProduct> = {
  opencti: "opencti",
  openaev: "openaev",
  openbas: "openaev",
  opencrq: "opencrq",
  opengrc: "opencrq",
};

const REGISTERING_PRODUCTS = ["opencti", "openaev", "opencrq"] as const;

export function productSwitcherEntries({
  registrations,
  hubRegistration,
  publicPages,
  hubRegistrationPath,
}: {
  registrations: readonly PlatformRegistration[] | undefined;
  hubRegistration: XtmHubRegistrationLike | null | undefined;
  /** The public page of each registering product that has one. */
  publicPages: Partial<Record<(typeof REGISTERING_PRODUCTS)[number], string>>;
  /** Where this user registers the platform on the XTM Hub, when they may. */
  hubRegistrationPath: string | null;
}): SwitcherEntry[] {
  const entries: SwitcherEntry[] = [];
  for (const product of REGISTERING_PRODUCTS) {
    const connected = (registrations ?? []).flatMap((registration): SwitcherEntry[] => {
      if (PRODUCT_OF_IDENTIFIER[registration.platform_identifier] !== product) return [];
      // The address is the registering platform's own claim, rendered as a
      // link: anything but http(s) is left out rather than followed.
      const href = safeHttpUrl(registration.platform_url);
      if (!href) return [];
      return [{
        id: registration.id,
        product,
        label: registration.platform_title || SUITE_PRODUCT_NAMES[product],
        href,
        target: registration.healthy ? "connected" : "stale",
      }];
    });
    const publicPage = publicPages[product];
    if (connected.length > 0) entries.push(...connected);
    else if (publicPage) entries.push({ id: product, product, label: SUITE_PRODUCT_NAMES[product], href: publicPage, target: "public" });
  }

  const hub = xtmHubPill(null, hubRegistration);
  const hubEntry = { id: "xtm_hub", product: "xtm_hub", label: SUITE_PRODUCT_NAMES.xtm_hub } as const;
  if (hub.present) entries.push({ ...hubEntry, href: hub.url, target: hub.healthy ? "connected" : "stale" });
  else if (hubRegistrationPath) entries.push({ ...hubEntry, to: hubRegistrationPath, target: "register" });
  // The default Hub (`XTM_HUB_DEFAULT_URL`) is known once the registration status answered.
  else if (hubRegistration) entries.push({ ...hubEntry, href: hubRegistration.default_hub_url, target: "public" });
  return entries;
}
