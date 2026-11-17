// SPDX-License-Identifier: Apache-2.0

import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { ProviderCatalogItem } from "@/types";

/**
 * The diagonal corner ribbon on an integration card.
 *
 * Certified or Preview comes from the catalog entry's `is_certified`, which
 * the backend serves from its one list (integration_catalog.CERTIFIED_PROVIDERS),
 * so qualifying a provider never takes a frontend change.
 *
 * Rendered by `ProviderCardBody` (integration-catalog-browser.tsx), which
 * owns the positioning context the `absolute` ribbon anchors to, so no card
 * variant can lose it.
 *
 * Custom providers carry no ribbon: "Preview" is a qualification XTM One makes
 * about its own catalog, and a customer's imported bundle is not ours to
 * qualify. Those cards already show their own "Custom" badge.
 */
export function IntegrationStatusRibbon({ item }: { item: ProviderCatalogItem }) {
  const { t_i18n } = useFormatter();
  if (item.is_custom) return null;

  const certified = item.is_certified === true;
  const label = certified ? t_i18n("Certified") : t_i18n("Preview");

  return (
    <div
      role="note"
      aria-label={certified ? t_i18n("Certified integration") : t_i18n("Preview integration")}
      data-testid="integration-status-ribbon"
      data-status={certified ? "certified" : "preview"}
      className="pointer-events-none absolute right-0 top-0 h-24 w-24 overflow-hidden rounded-tr-sm"
    >
      <div
        aria-hidden="true"
        className={cn(
          "absolute -right-[38px] top-[22px] w-36 rotate-45 border-y py-0.5 text-center content-compact font-semibold uppercase tracking-[0.3px]",
          // A 96 px box and a 144 px band so the longest label ("VISTA PREVIA",
          // "CERTIFICADO") clears the clip in every locale. 12 px (the design standards floor), on py-0.5 so the band keeps its
          // former thickness and the provider name stays clear of it. The text needs 4.5:1 against its fill in every mode and theme:
          // both foregrounds are set for that, and recomputed from the
          // fill when an admin configures a custom accent or background.
          certified
            ? "border-accent/30 bg-accent text-accent-foreground"
            : "border-elevation-subtle bg-elevation-highlight text-on-highlight",
        )}
      >
        {label}
      </div>
    </div>
  );
}
