// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";

import type { SelectOption } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import { modelChoiceValue, modelOptionLabel, parseModelChoice, providerDisplayName } from "@/lib/utils";
import type { AvailableModel, LLMProvider } from "@/types";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * The options of a model picker, for `SearchableSelect`.
 *
 * A model id can be advertised by several provider configs, so each offering
 * sits under its config's header with the bare id as its row, and the field
 * names both once picked (`gpt-4o (OpenAI)`): the row is short, the choice is
 * never ambiguous. Typing a provider's name finds its models.
 *
 * An older saved value pins no provider and so matches no offering; it
 * is offered as its own entry rather than snapped to an arbitrary provider,
 * which is the guess the provider pin exists to remove.
 */
export function modelSelectOptions(
  options: readonly AvailableModel[],
  providers: LLMProvider[] | null | undefined,
  value: string | null | undefined,
  t_i18n: Translate,
): SelectOption[] {
  const offerings: SelectOption[] = options.map((m) => ({
    value: modelChoiceValue(m.model, m.provider_id),
    label: m.model,
    selectedLabel: modelOptionLabel(m, providers),
    group: providerDisplayName(m.provider, m.provider_id, providers),
    keywords: [m.provider, PROVIDER_DISPLAY_NAMES[m.provider]],
  }));
  if (!value || offerings.some((offering) => offering.value === value)) return offerings;

  const { model, providerId } = parseModelChoice(value);
  if (!model) return offerings;
  const stored = providerId ? providers?.find((p) => p.id === providerId) : undefined;
  const saved: SelectOption = stored
    ? {
        value,
        label: model,
        selectedLabel: modelOptionLabel({ model, provider: stored.provider, provider_id: stored.id }, providers),
        group: providerDisplayName(stored.provider, stored.id, providers),
      }
    : { value, label: providerId ? model : t_i18n("{model}: provider not pinned", { model }) };
  return [saved, ...offerings];
}

/** `modelSelectOptions`, memoised and translated. */
export function useModelSelectOptions(
  options: readonly AvailableModel[] | null | undefined,
  providers: LLMProvider[] | null | undefined,
  value: string | null | undefined,
): SelectOption[] {
  const { t_i18n } = useFormatter();
  return useMemo(() => modelSelectOptions(options ?? [], providers, value, t_i18n), [options, providers, value, t_i18n]);
}
