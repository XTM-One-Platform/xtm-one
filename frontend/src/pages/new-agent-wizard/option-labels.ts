/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Human labels for the ids the agent wizard draft holds.
 *
 * The canvas and the step pickers name each selected tool, knowledge base,
 * skill or handover agent through the flow node catalog, which lists a
 * knowledge base created from the wizard at once (`lib/flow-node-catalog.ts`).
 * A selection the catalog does not list is one of two things:
 *
 * - the catalog has not loaded (still fetching, or its first load failed):
 *   nothing can be said about the id, so it is shown as is;
 * - it is no longer available to the user (deleted, unshared, or restored from
 *   an older draft): it is named as such, and its picker offers it for removal
 *   (`MultiSelectListField`'s `showUnlisted`) - creating the agent with it
 *   would be refused.
 */
interface LabelledOption {
  value: string;
  label: string;
}

/**
 * The catalog's label for `value`, else `unavailableLabel` - pass it only once
 * the catalog has loaded - else the id itself.
 */
export function resolveOptionLabel(
  options: readonly LabelledOption[],
  value: string,
  unavailableLabel?: string,
): string {
  const option = options.find((o) => o.value === value);
  if (option) return option.label || value;
  return unavailableLabel ?? value;
}
