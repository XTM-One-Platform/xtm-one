// SPDX-License-Identifier: Apache-2.0

/**
 * When the CTEM cases were last recomputed (dev-docs/ctem-cases.md s.4.3).
 * Cases are inferred from OpenCTI and OpenAEV and recompute by themselves, so
 * the Cases header carries no button: a quiet "Updated 4 min ago" (exact time
 * in the shared tooltip, on hover and on keyboard focus) and, when a product
 * did not answer since, a muted line naming it with a link to the integration
 * the reader can fix. Never a toast or a modal for a routine refresh.
 */
import { Link } from "react-router-dom";
import { Tip } from "@/components/ui/tooltip";
import type { CtemCasesFreshness } from "@/hooks/api/useCtemCasesApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { missingProductKeys, missingProducts, outageIsCurrent, outageLink } from "./cases-freshness";

export function CasesFreshness({ freshness, now }: { freshness: CtemCasesFreshness | undefined; now: number }) {
  const { t_i18n, intl } = useFormatter();
  const { data: integrations } = useIntegrations();
  if (!freshness) return null;
  const outage = outageIsCurrent(freshness);
  const products = missingProducts(freshness);
  return (
    <div className="flex flex-col items-end gap-0.5 text-right content-caption text-default-secondary" data-testid="ctem-cases-freshness">
      {freshness.updated_at ? (
        <Tip content={intl.formatDate(freshness.updated_at, { dateStyle: "medium", timeStyle: "short" })}>
          <span tabIndex={0} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus">
            {t_i18n("Updated {when}", { when: formatTimeAgo(intl, freshness.updated_at, now) })}
          </span>
        </Tip>
      ) : (
        <span>{t_i18n("Not computed yet")}</span>
      )}
      {outage && freshness.missing_at && (
        <span>
          {t_i18n(
            "{count, plural, one {{products} did not answer at {time}, cases unchanged.} other {{products} did not answer at {time}, cases unchanged.}}",
            {
              count: products.length,
              products: intl.formatList(products, { type: "conjunction" }),
              time: intl.formatTime(freshness.missing_at, { timeStyle: "short" }),
            },
          )}{" "}
          <Link to={outageLink(missingProductKeys(freshness), integrations)} className="underline">
            {products.length === 1 ? t_i18n("Open the {product} integration", { product: products[0] }) : t_i18n("Integrations")}
          </Link>
        </span>
      )}
    </div>
  );
}
