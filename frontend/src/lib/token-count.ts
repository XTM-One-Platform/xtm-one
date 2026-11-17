// SPDX-License-Identifier: Apache-2.0

/**
 * Token counts as an administrator types them: `128000`, `128,000`, `128K`,
 * `1.05M`. The shorthand is decimal, as the vendors write it (`128K` is
 * 128,000 and `1.05M` is 1,050,000), and the count is stored as the whole
 * number of tokens. Mirrors `parse_token_count` in
 * `backend/app/llm/model_limits.py`, which validates what is saved.
 */

/** The largest context window accepted (`MAX_CONTEXT_WINDOW_TOKENS`). */
export const MAX_CONTEXT_WINDOW_TOKENS = 100_000_000;

const SHORTHAND = /^(\d+)(?:[.,](\d+))? *([km])$/i;
// Plain digits, or digits grouped by three with one separator used throughout.
const GROUPED = /^\d{1,3}([ ,_\u00a0\u202f])\d{3}(?:\1\d{3})*$/;
const MULTIPLIERS: Record<string, number> = { k: 1_000, m: 1_000_000 };
// Longer than any count up to the bound can be typed (`_MAX_TOKEN_TEXT_LENGTH`).
const MAX_TEXT_LENGTH = 32;

/** The result of reading a typed count: empty, a count, or not a count. */
type TokenCountInput =
  | { kind: "empty" }
  | { kind: "tokens"; tokens: number }
  | { kind: "invalid" };

function inBounds(tokens: number): TokenCountInput {
  return Number.isSafeInteger(tokens) && tokens > 0 && tokens <= MAX_CONTEXT_WINDOW_TOKENS
    ? { kind: "tokens", tokens }
    : { kind: "invalid" };
}

export function parseTokenCount(text: string): TokenCountInput {
  const value = text.trim();
  if (!value) return { kind: "empty" };
  if (value.length > MAX_TEXT_LENGTH) return { kind: "invalid" };
  const shorthand = SHORTHAND.exec(value);
  if (shorthand) {
    const [, whole, fraction = "", suffix] = shorthand;
    const scale = MULTIPLIERS[suffix.toLowerCase()];
    const digits = Math.log10(scale);
    // Exact decimal arithmetic: 1.05 * 1e6 in floating point is not 1050000.
    if (fraction.replace(/0+$/, "").length > digits) return { kind: "invalid" };
    return inBounds(Number(whole) * scale + Number(fraction.padEnd(digits, "0").slice(0, digits) || "0"));
  }
  if (/^\d+$/.test(value)) return inBounds(Number(value));
  if (GROUPED.test(value)) return inBounds(Number(value.replace(/\D/g, "")));
  return { kind: "invalid" };
}

function shortForm(tokens: number, scale: number, suffix: string, maxDecimals: number): string | null {
  const scaled = tokens / scale;
  for (let decimals = 0; decimals <= maxDecimals; decimals++) {
    const text = scaled.toFixed(decimals);
    if (Math.round(Number(text) * scale) === tokens) return `${Number(text)}${suffix}`;
  }
  return null;
}

/**
 * A count as the shortest shorthand that reads back to the same number of
 * tokens (`1050000` -> `1.05M`, `128000` -> `128K`), else the plain number
 * (`131072`). `parseTokenCount(formatTokenCount(n))` is always `n`.
 */
export function formatTokenCount(tokens: number): string {
  if (tokens >= 1_000_000) {
    const millions = shortForm(tokens, 1_000_000, "M", 2);
    if (millions) return millions;
  }
  if (tokens >= 1_000) {
    const thousands = shortForm(tokens, 1_000, "K", 1);
    if (thousands) return thousands;
  }
  return String(tokens);
}
