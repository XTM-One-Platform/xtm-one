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
 * The pack options a Deep Investigation picker confirms.
 *
 * Kept out of the component so the rule is testable: every option of every
 * CONFIRMED pack is sent, its default included, so the run records what was
 * chosen rather than what a default happened to be; an unticked pack sends
 * nothing; a value the pack no longer offers falls back to its default.
 */
import type { PackOption, SourcePackOption, SourcePackOptions } from "../../types/index.ts";

/** Each option's default, by pack slug. */
export function defaultPackOptions(packs: SourcePackOption[]): SourcePackOptions {
  const out: SourcePackOptions = {};
  for (const pack of packs) {
    if (!pack.options?.length) continue;
    out[pack.slug] = Object.fromEntries(pack.options.map((o) => [o.key, o.default]));
  }
  return out;
}

/** What the confirm sends: the options of the confirmed packs only. */
export function confirmedPackOptions(
  packs: SourcePackOption[],
  confirmed: string[],
  chosen: SourcePackOptions,
): SourcePackOptions {
  const out: SourcePackOptions = {};
  for (const pack of packs) {
    if (!confirmed.includes(pack.slug) || !pack.options?.length) continue;
    out[pack.slug] = Object.fromEntries(
      pack.options.map((o) => {
        const value = chosen[pack.slug]?.[o.key];
        const offered = o.choices.some((c) => c.value === value);
        return [o.key, offered && value !== undefined ? value : o.default];
      }),
    );
  }
  return out;
}

/**
 * The description shown under an option for its current value, or null when
 * the choice has none: an empty string is not a catalog key, and rendering it
 * through `t_i18n` throws.
 */
export function choiceHint(option: PackOption, current: string): string | null {
  const choice = option.choices.find((c) => c.value === current);
  return choice?.description ? choice.description : null;
}
