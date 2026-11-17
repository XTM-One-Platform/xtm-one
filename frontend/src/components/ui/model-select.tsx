// SPDX-License-Identifier: Apache-2.0

import { Spinner } from "@filigran/design-system";

import { useModelSelectOptions } from "@/components/ui/model-select-options";
import { SearchableSelect, type SearchableSelectProps } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";
import type { AvailableModel, LLMProvider } from "@/types";

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

export type ModelSelectProps = DistributiveOmit<SearchableSelectProps, "options"> & {
  /** Model offerings, as returned by the models API. */
  models: readonly AvailableModel[] | null | undefined;
  /** Provider configs, used to name each offering's config. */
  providers?: LLMProvider[] | null;
};

/**
 * A model picker: the offerings under their provider config's
 * header, both sorted, searchable by model id or provider, and named
 * `model (provider)` once picked. "Default" / "None" entries go in `pinned`.
 */
export function ModelSelect({ models, providers, value, ...props }: ModelSelectProps) {
  const { t_i18n } = useFormatter();
  const options = useModelSelectOptions(models, providers, value);
  return (
    <SearchableSelect
      placeholder={t_i18n("Search models...")}
      pinnedGroup={t_i18n("Defaults")}
      {...props}
      options={options}
      value={value}
    />
  );
}

/**
 * Said beside a capability's model picker while its providers are asked for
 * their models again: the picker refetches its list when it opens.
 */
export function ModelListRefreshing({ active }: { active: boolean }) {
  const { t_i18n } = useFormatter();
  if (!active) return null;
  return (
    <p role="status" className="flex items-center gap-1.5 content-caption text-default-secondary">
      <Spinner size="sm" tone="inherit" /> {t_i18n("Reading the models the providers serve now...")}
    </p>
  );
}
