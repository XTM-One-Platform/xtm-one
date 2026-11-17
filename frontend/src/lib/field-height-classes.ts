// SPDX-License-Identifier: Apache-2.0

/**
 * The height a page gives a picker, as the classes its visible field needs.
 *
 * `SearchableSelect` hands its `className` to the plain select's trigger, which
 * it sizes, and to the combobox root, which it does not: the combobox field
 * keeps the design system's `min-h-9` floor and renders 36 px where the plain
 * select renders the page's `h-8`. This keeps the height utilities of the
 * class list for the field and pairs each `h-*` with the `min-h-*` of the same
 * size, which replaces the library's floor (tailwind-merge resolves `min-h-*`
 * against `min-h-*`, never against `h-*`). Responsive and state prefixes and
 * the `!` modifier are kept. No height in the list: nothing, the field keeps
 * the library's default.
 */
export function fieldHeightClasses(className: string | undefined): string | undefined {
  if (!className) return undefined;
  const classes: string[] = [];
  for (const token of className.split(/\s+/)) {
    const match = /^((?:[\w[\]&-]+:)*)(!?)((?:min-|max-)?)h-(\S+)$/.exec(token);
    if (!match) continue;
    const [, prefix, important, bound, size] = match;
    classes.push(token);
    if (!bound) classes.push(`${prefix}${important}min-h-${size}`);
  }
  return classes.length ? classes.join(" ") : undefined;
}
