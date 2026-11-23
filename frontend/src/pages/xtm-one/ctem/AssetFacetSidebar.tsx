// SPDX-License-Identifier: Apache-2.0

/**
 * The faceted sidebar of CTEM > Inventory > Assets (#1501, Sam 23 Nov 2026: "basket style filter ... in basket at
 * least categories, criticality, source and internet facing, subcategories"). Each value carries its icon and the
 * server's count, which applies the search and the other facets picked; a subcategory sits under its category. The
 * selection lives in the URL (`a_f_<facet>`), the keys the table reads, so a link and `?a_f_category=ai_target` keep
 * working.
 */
import { useSearchParams } from "react-router-dom";
import { FacetBranch, FacetCheckbox, FacetGroup, FacetSidebar } from "@/components/ui/facet-sidebar";
import type { AssetFacetValue, AssetFacets, AssetFilterKey } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { CategoryMark, SourceLogo } from "./inventory-icons";
import { assetFieldLabel, sourceLabel } from "./inventory";

/** The facets of the sidebar, in the order it shows them; a subcategory is drawn under its category. */
export const SIDEBAR_FACETS: readonly AssetFilterKey[] = ["category", "subcategory", "criticality", "internet_facing", "source"];

/** The URL key of one facet of a table whose keys start with `prefix`. */
export function facetParam(prefix: string, facet: AssetFilterKey): string {
  return `${prefix}f_${facet}`;
}

/** `params` with `value` added to or removed from `facet`, back on the first page. */
export function toggleFacetParam(params: URLSearchParams, prefix: string, facet: AssetFilterKey, value: string): URLSearchParams {
  const next = new URLSearchParams(params);
  const name = facetParam(prefix, facet);
  const values = next.getAll(name);
  next.delete(name);
  const kept = values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
  kept.forEach((v) => next.append(name, v));
  next.delete(`${prefix}page`);
  return next;
}

/** `params` without the values of `facets`, back on the first page. */
export function clearFacetParams(params: URLSearchParams, prefix: string, facets: readonly AssetFilterKey[]): URLSearchParams {
  const next = new URLSearchParams(params);
  facets.forEach((facet) => next.delete(facetParam(prefix, facet)));
  next.delete(`${prefix}page`);
  return next;
}

/** A facet's values with the picked ones the counts no longer hold kept at 0, so a pick can always be undone. */
function withPicked(values: readonly AssetFacetValue[], picked: readonly string[]): AssetFacetValue[] {
  const known = new Set(values.map((v) => v.value));
  return [...values, ...picked.filter((p) => !known.has(p)).map((value) => ({ value, count: 0 }))];
}

const GROUP_LABELS: Record<Exclude<AssetFilterKey, "subcategory">, string> = {
  category: "Category",
  criticality: "Criticality",
  internet_facing: "Internet facing",
  source: "Source",
};

export function AssetFacetSidebar({
  facets, picked, prefix,
}: {
  facets: AssetFacets | undefined;
  picked: Record<AssetFilterKey, string[]>;
  prefix: string;
}) {
  const { t_i18n } = useFormatter();
  const [, setSearchParams] = useSearchParams();
  const toggle = (facet: AssetFilterKey, value: string) => setSearchParams((prev) => toggleFacetParam(prev, prefix, facet, value), { replace: true });
  const clear = (keys: readonly AssetFilterKey[]) => setSearchParams((prev) => clearFacetParams(prev, prefix, keys), { replace: true });
  const hasFilters = SIDEBAR_FACETS.some((facet) => picked[facet].length > 0);
  const subcategories = withPicked(facets?.subcategory ?? [], picked.subcategory);
  const categories = withPicked(facets?.category ?? [], picked.category);

  const simple = (facet: "criticality" | "internet_facing" | "source", divided: boolean) => {
    const values = withPicked(facets?.[facet] ?? [], picked[facet]);
    if (values.length === 0) return null;
    return (
      <FacetGroup
        key={facet}
        label={t_i18n(GROUP_LABELS[facet])}
        divided={divided}
        testId={`asset-facet-${facet}`}
        onClear={picked[facet].length > 0 ? () => clear([facet]) : undefined}
      >
        {values.map(({ value, count }) => (
          <FacetCheckbox
            key={value}
            checked={picked[facet].includes(value)}
            count={count}
            label={facet === "source" ? sourceLabel(t_i18n, value) : assetFieldLabel(t_i18n, facet, value)}
            leading={facet === "source" ? <SourceLogo source={value} /> : undefined}
            onToggle={() => toggle(facet, value)}
          />
        ))}
      </FacetGroup>
    );
  };

  return (
    <FacetSidebar hasFilters={hasFilters} onClearAll={() => clear(SIDEBAR_FACETS)} testId="ctem-inventory-asset-facets">
      {categories.length > 0 && (
        <FacetGroup
          label={t_i18n(GROUP_LABELS.category)}
          testId="asset-facet-category"
          onClear={picked.category.length + picked.subcategory.length > 0 ? () => clear(["category", "subcategory"]) : undefined}
        >
          {categories.map(({ value, count }) => {
            const label = assetFieldLabel(t_i18n, "category", value);
            const row = (
              <FacetCheckbox
                checked={picked.category.includes(value)}
                count={count}
                label={label}
                leading={<CategoryMark category={value} />}
                onToggle={() => toggle("category", value)}
              />
            );
            const children = subcategories.filter((s) => s.category === value);
            if (children.length === 0) return <div key={value}>{row}</div>;
            return (
              <FacetBranch
                key={value}
                row={row}
                expandLabel={t_i18n("Subcategories of {category}", { category: label })}
                defaultOpen={children.some((s) => picked.subcategory.includes(s.value))}
              >
                {children.map((sub) => (
                  <FacetCheckbox
                    key={sub.value}
                    indent
                    checked={picked.subcategory.includes(sub.value)}
                    count={sub.count}
                    label={assetFieldLabel(t_i18n, "subcategory", sub.value)}
                    leading={<CategoryMark category={value} subcategory={sub.value} />}
                    onToggle={() => toggle("subcategory", sub.value)}
                  />
                ))}
              </FacetBranch>
            );
          })}
          {/* A picked subcategory the counts no longer place under a category stays listed, so it can be unpicked. */}
          {subcategories
            .filter((s) => !s.category)
            .map((sub) => (
              <FacetCheckbox
                key={`orphan-${sub.value}`}
                indent
                checked
                count={sub.count}
                label={assetFieldLabel(t_i18n, "subcategory", sub.value)}
                onToggle={() => toggle("subcategory", sub.value)}
              />
            ))}
        </FacetGroup>
      )}
      {simple("criticality", true)}
      {simple("internet_facing", true)}
      {simple("source", true)}
    </FacetSidebar>
  );
}
