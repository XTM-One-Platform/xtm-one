/**
 * French typography of the fr catalog: a high punctuation mark ("?",
 * "!", ";", ":") that ends a word takes a no-break space before it, never a
 * plain one, or a wrapping dialog title can leave the mark alone at the start
 * of the next line. "?", "!" and ";" take the narrow no-break space (U+202F),
 * ":" the no-break space (U+00A0).
 *
 * Only a mark that follows a word (a letter, a digit, a currency sign, a closing
 * quote, bracket or ellipsis) and is followed by the end, a space, a closing
 * brace, quote or bracket is read as punctuation: "? pour l'aide" after a
 * comma, "::1" or "otpauth:// ;" are left alone.
 */

/** A plain space between a word and a high punctuation mark that ends it. */
export const PLAIN_SPACE_BEFORE_MARK = /(?<=[\p{L}\p{N}$€%»"'’)\]}…*.]) ([?!;:])(?=$|[\s}"'»)\]])/gu;

/** The space each mark takes. */
export function noBreakSpaceFor(mark) {
  return mark === ":" ? "\u00a0" : "\u202f";
}

/** Every value of `catalog` with a plain space before a mark: `{ key, mark }`, once per mark. */
export function frenchSpacingProblems(catalog) {
  const problems = [];
  for (const [key, message] of Object.entries(catalog)) {
    for (const match of String(message).matchAll(PLAIN_SPACE_BEFORE_MARK)) {
      problems.push({ key, mark: match[1] });
    }
  }
  return problems;
}

/** `message` with each plain space before a mark replaced by the space it takes. */
export function fixFrenchSpacing(message) {
  return message.replace(PLAIN_SPACE_BEFORE_MARK, (_, mark) => `${noBreakSpaceFor(mark)}${mark}`);
}
