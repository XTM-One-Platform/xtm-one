// SPDX-License-Identifier: Apache-2.0

/**
 * The longest text a Tip shows. A tooltip is a hint, not a reader: a
 * 965-character skill description rendered 300x376 and its top edge left the
 * viewport at 1024. Past this many characters the text is cut at a word and
 * ends with an ellipsis, so every Tip stays a few lines tall.
 */
const TIP_MAX_CHARS = 280;

/** `text` cut at a word to at most `max` characters, with an ellipsis when cut. */
export function capTipText(text: string, max: number = TIP_MAX_CHARS): string {
  if (text.length <= max) return text;
  const head = text.slice(0, max - 1);
  const space = head.lastIndexOf(" ");
  return `${(space > max * 0.6 ? head.slice(0, space) : head).trimEnd()}\u2026`;
}
