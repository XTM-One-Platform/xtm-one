// SPDX-License-Identifier: Apache-2.0

/**
 * How a case states its primary assets (#2635): the ones a manual or OpenCRQ link
 * proves and the ones only an inventory rule, a name match or the evidence
 * infers, each said as such, never "0 primary assets" when the gate inferred
 * some. A case that matched no asset and no primary asset names what put it in
 * scope instead (`opened_on`, the kinds of the gate decision's scope sources).
 */
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The scope source kinds of the gate decision, as their catalog labels. */
const SCOPE_KIND_LABEL: Record<string, string> = {
  asset: "Asset",
  asset_group: "Asset group",
  primary_asset: "Primary asset",
  owned_domain: "Owned domain",
  risk_scenario: "Risk scenario",
};

type Counts = Pick<CtemCaseSummary, "primary_asset_count" | "inferred_primary_asset_count" | "opened_on" | "asset_count">;

/** The line under the Assets figure, `null` when the counts are unknown for this reader. */
export function primaryAssetsLine(c: Counts, t_i18n: Translate): string | null {
  if (c.primary_asset_count == null) return null;
  const proven = c.primary_asset_count;
  const inferred = c.inferred_primary_asset_count ?? 0;
  if (proven > 0 && inferred > 0) {
    return t_i18n("{proven, plural, one {# proven primary asset} other {# proven primary assets}}, {inferred, plural, one {# inferred} other {# inferred}}", {
      proven,
      inferred,
    });
  }
  if (inferred > 0) {
    return t_i18n("{count, plural, one {# inferred primary asset} other {# inferred primary assets}}", { count: inferred });
  }
  const kinds = (c.opened_on ?? []).filter((k) => k in SCOPE_KIND_LABEL);
  if (proven === 0 && !c.asset_count && kinds.length > 0) {
    return t_i18n("No asset matched; opened on: {reasons}", { reasons: kinds.map((k) => t_i18n(SCOPE_KIND_LABEL[k])).join(", ") });
  }
  return t_i18n("{count, plural, one {# primary asset} other {# primary assets}}", { count: proven });
}

/** The Primary assets cell of the cases list: the proven count, else the inferred one said as inferred. */
export function primaryAssetsCell(c: Counts, t_i18n: Translate, format: (n: number | null) => string): string {
  const inferred = c.inferred_primary_asset_count ?? 0;
  if (c.primary_asset_count === 0 && inferred > 0) {
    return t_i18n("{count, plural, one {# inferred} other {# inferred}}", { count: inferred });
  }
  return format(c.primary_asset_count);
}
