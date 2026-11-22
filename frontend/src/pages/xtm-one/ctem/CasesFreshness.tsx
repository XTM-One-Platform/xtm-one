// SPDX-License-Identifier: Apache-2.0

/**
 * When the CTEM cases were last recomputed (dev-docs/ctem-cases.md s.4.3).
 * Cases are inferred from OpenCTI and OpenAEV and recompute by themselves, so
 * the Cases header carries no button: a quiet "Updated 4 min ago" (exact time
 * on hover) and, when a product did not answer since, a muted line naming it
 * with a link to the integrations. Never a toast or a modal for a routine
 * refresh.
 */
import { Link } from "react-router-dom";
import type { CtemCasesFreshness } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";
import { missingProducts, outageIsCurrent } from "./cases-freshness";

export function CasesFreshness({ freshness, now }: { freshness: CtemCasesFreshness | undefined; now: number }) {
  const { t_i18n, intl, locale } = useFormatter();
  if (!freshness) return null;
  const exact = (iso: string) => intl.formatDate(iso, { dateStyle: "medium", timeStyle: "short" });
  const outage = outageIsCurrent(freshness);
  return (
    <div className="flex flex-col items-end gap-0.5 text-right content-caption text-default-secondary" data-testid="ctem-cases-freshness">
      {freshness.updated_at ? (
        <span title={exact(freshness.updated_at)}>{t_i18n("Updated {when}", { when: timeAgo(freshness.updated_at, locale, now) })}</span>
      ) : (
        <span>{t_i18n("Not computed yet")}</span>
      )}
      {outage && freshness.missing_at && (
        <span>
          {t_i18n("{products} did not answer at {time}, cases unchanged.", {
            products: missingProducts(freshness).join(", "),
            time: intl.formatTime(freshness.missing_at, { timeStyle: "short" }),
          })}{" "}
          <Link to="/settings/integrations" className="underline">
            {t_i18n("Integrations")}
          </Link>
        </span>
      )}
    </div>
  );
}
