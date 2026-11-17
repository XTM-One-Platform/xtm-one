// SPDX-License-Identifier: Apache-2.0

/**
 * How every picker orders and searches its options.
 *
 * One rule for every list a person picks from, so two pickers never disagree
 * on where "ElevenLabs" goes: alphabetical in the interface language, case and
 * accents ignored, numbers read by value ("Agent 2" before "Agent 10"). A list
 * whose order means something (slides, severities, time ranges) is simply not
 * sorted; it never gets an order of its own here.
 *
 * Matching is word by word: every word typed must appear somewhere in the
 * option's texts, in any order, accents and case ignored - "exec assist" finds
 * "[EMT & ExCo] Executive Assistant", "equipe" finds "Équipe".
 *
 * Pure (no React, no alias imports) so `node --test` runs it as is.
 */

const collators = new Map<string, Intl.Collator>();

/** The collator of a locale (the runtime's own when none is given), built once. */
export function labelCollator(locale?: string | null): Intl.Collator {
  const key = locale ?? "";
  let collator = collators.get(key);
  if (!collator) {
    collator = new Intl.Collator(locale || undefined, { sensitivity: "base", numeric: true });
    collators.set(key, collator);
  }
  return collator;
}

/** A copy of `items` in label order. Stable: labels that compare equal keep their order. */
export function sortByLabel<T>(items: readonly T[], label: (item: T) => string, locale?: string | null): T[] {
  const collator = labelCollator(locale);
  return [...items].sort((a, b) => collator.compare(label(a), label(b)));
}

/**
 * The options of a filter in the order a person scans them: the `pinned` values
 * first, in the order given ("All", "None", "Me"), then the rest by label.
 * `ordered` keeps the order given, for an ordinal scale only (severity,
 * priority, criticality, a time range), said by a comment at the call site.
 */
export function sortFilterOptions<T extends { value: string; label: string }>(
  options: readonly T[],
  { locale, ordered = false, pinned = [] }: { locale?: string | null; ordered?: boolean; pinned?: readonly string[] } = {},
): T[] {
  if (ordered) return [...options];
  const first = pinned.flatMap((value) => options.filter((option) => option.value === value));
  const rest = options.filter((option) => !pinned.includes(option.value));
  return [...first, ...sortByLabel(rest, (option) => option.label, locale)];
}

/**
 * A copy of `items` ordered by group, then by label inside each group. Items
 * without a group come first, as a list's own entries come before its sections.
 */
export function sortByGroupThenLabel<T>(
  items: readonly T[],
  group: (item: T) => string | null | undefined,
  label: (item: T) => string,
  locale?: string | null,
): T[] {
  const collator = labelCollator(locale);
  return [...items].sort((a, b) => {
    const ga = group(a) ?? "";
    const gb = group(b) ?? "";
    if (ga !== gb) {
      if (!ga) return -1;
      if (!gb) return 1;
      const byGroup = collator.compare(ga, gb);
      if (byGroup !== 0) return byGroup;
    }
    return collator.compare(label(a), label(b));
  });
}

/**
 * A copy of `items` with the groups in the order they first appear and the
 * labels sorted inside each one: for sections whose order is meaningful
 * (a provider listing an account's own voices first) over entries that are not.
 */
export function sortWithinGroups<T>(
  items: readonly T[],
  group: (item: T) => string | null | undefined,
  label: (item: T) => string,
  locale?: string | null,
): T[] {
  const sections = new Map<string, T[]>();
  for (const item of items) {
    const key = group(item) ?? "";
    const section = sections.get(key);
    if (section) section.push(item);
    else sections.set(key, [item]);
  }
  return [...sections.values()].flatMap((section) => sortByLabel(section, label, locale));
}

/** Lower case with the accents taken off: what a query and a label are compared as. */
export function foldForSearch(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/** Whether every word of `query` appears in one of `texts`. An empty query matches everything. */
export function matchesQuery(query: string, texts: readonly (string | null | undefined)[]): boolean {
  const words = foldForSearch(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = foldForSearch(texts.filter((text): text is string => !!text).join("\n"));
  return words.every((word) => haystack.includes(word));
}
