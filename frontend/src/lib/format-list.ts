// SPDX-License-Identifier: Apache-2.0

/**
 * Join a few names in the viewer's language ("a, b, and c", "a et b",
 * "a、b") with Intl.ListFormat, instead of a literal ", " that reads
 * as English in every locale. Falls back to ", " where the runtime has no
 * Intl.ListFormat or does not know the locale.
 *
 * Intl.ListFormat is ES2021 and the frontend's tsconfig lib is ES2020, so the
 * constructor is read through a local type instead of widening the lib.
 */
type ListFormatCtor = new (
  locale: string,
  options: { style: "long"; type: "conjunction" },
) => { format: (items: readonly string[]) => string };

export function formatList(items: readonly string[], locale: string): string {
  const ListFormat = (Intl as unknown as { ListFormat?: ListFormatCtor }).ListFormat;
  if (!ListFormat) return items.join(", ");
  try {
    return new ListFormat(locale, { style: "long", type: "conjunction" }).format(items);
  } catch {
    return items.join(", ");
  }
}
