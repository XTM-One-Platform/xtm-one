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
 * Whether a template theme value is safe to paint with - the mirror of
 * `theme_value_problem()` in `backend/app/services/presentations.py`, which
 * stays the authority: it runs on every template write and on every deck and
 * report read.
 *
 * The template PREVIEW builds its palette on the client, from the template as
 * stored or as being edited (`previewSpec.ts`), which no backend read has
 * filtered. A palette value lands in the CSS custom properties the deck paints
 * with (`background: var(--deck-bg)`), so a value that loads an image would
 * make the browser of whoever looks at the preview fetch an outside address. A
 * browser loads a resource only through a CSS FUNCTION (`url()`, `image-set()`,
 * `cross-fade()`, `image()`, `element()`, ...), never through a keyword or a
 * quoted string, so the rule is an allow-list of the functions that cannot
 * load anything, read through the escapes and the case a browser reads them
 * through. `SAFE_THEME_FUNCTIONS`, `MAX_THEME_VALUE_CHARS` and
 * `THEME_FILE_ID_KEYS` are pinned to the backend's by a backend test; the trim
 * set and the file-id form by the shared case table both sides read.
 */

/** The longest theme value a template may carry (mirrors `MAX_THEME_VALUE_CHARS`). */
export const MAX_THEME_VALUE_CHARS = 120;

/** Theme keys naming a template asset by UUID rather than a CSS token (mirrors `THEME_FILE_ID_KEYS`). */
export const THEME_FILE_ID_KEYS: readonly string[] = ["logo_file_id", "logo_light_file_id", "mark_file_id"];

/** The CSS functions a theme value may call: colours, math, `var()` and gradients - none can load anything.
 * @public
 */
export const SAFE_THEME_FUNCTIONS: ReadonlySet<string> = new Set([
  "rgb",
  "rgba",
  "hsl",
  "hsla",
  "hwb",
  "lab",
  "lch",
  "oklab",
  "oklch",
  "color",
  "color-mix",
  "light-dark",
  "calc",
  "min",
  "max",
  "clamp",
  "var",
  "linear-gradient",
  "radial-gradient",
  "conic-gradient",
  "repeating-linear-gradient",
  "repeating-radial-gradient",
  "repeating-conic-gradient",
  "-webkit-linear-gradient",
  "-webkit-radial-gradient",
  "-webkit-conic-gradient",
  "-webkit-repeating-linear-gradient",
  "-webkit-repeating-radial-gradient",
  "-webkit-repeating-conic-gradient",
  "-moz-linear-gradient",
  "-moz-radial-gradient",
  "-moz-conic-gradient",
  "-moz-repeating-linear-gradient",
  "-moz-repeating-radial-gradient",
  "-moz-repeating-conic-gradient",
  "-webkit-gradient",
]);

/** The arguments of the legacy `-webkit-gradient()`: functions only inside it. */
const WEBKIT_GRADIENT_ARGUMENTS: ReadonlySet<string> = new Set(["from", "to", "color-stop"]);

/**
 * What a value is trimmed of at both ends before it is judged and served
 * (mirrors `THEME_VALUE_TRIM_CHARS`): JavaScript's own `trim()` set plus
 * U+001C-U+001F and U+0085, which Python's `str.strip()` removes too.
 */
const TRIM_CHARS: ReadonlySet<string> = new Set(
  "\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff",
);

const CSS_ESCAPE = /\\(?:([0-9A-Fa-f]{1,6})[ \t\n\r\f]?|([\s\S])|$)/g;
const CSS_WHITESPACE = /[ \t\n\r\f]/;
const FUNCTION_NAME_CHAR = /[a-z0-9_-]/;
/** A name CSS calls: one with a letter or `_`, or a dashed (author-defined) one such as `--1`. */
const NAMES_A_FUNCTION = /[a-z_]|^--/;
/** Anything but what a colour, a gradient or a font stack is written with: `;`, braces, `*`, `@`, `\`, `:`, `<`, control and non-ASCII characters. */
const DISALLOWED_CHAR = /[^A-Za-z0-9#(),.%_'"/ -]/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `value` with its CSS escapes decoded: `\75 rl(`, `u\rl(` and `\000075rl(` all read `url(`. */
function decodeCssEscapes(value: string): string {
  return value.replace(CSS_ESCAPE, (_match, digits: string | undefined, char: string | undefined) => {
    if (digits === undefined) return char ?? "\ufffd";
    const code = Number.parseInt(digits, 16);
    if (code === 0 || (code >= 0xd800 && code <= 0xdfff) || code > 0x10ffff) return "\ufffd";
    return String.fromCodePoint(code);
  });
}

/**
 * Whether every `(` in `text` (decoded, lower-cased) opens an allowed function
 * written right before it - or a bare group after no name, as in
 * `calc((100% - 8px) / 2)` - and every parenthesis is closed. A space between a
 * name and its `(` is refused: CSS calls no function across one. A quoted
 * string is text, never a call (`"Foo (Bar)", sans-serif` is a font family),
 * so it is skipped; a string left open is refused. Reading the source is enough:
 * `var()` substitutes tokens, so it never turns a name and a later `(` into a
 * call (see `_theme_function_problem`).
 */
function callsOnlySafeFunctions(text: string): boolean {
  const open: string[] = [];
  let quote: string | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text.charAt(index);
    if (quote !== null) {
      if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (char === ")") {
      if (open.length === 0) return false;
      open.pop();
      continue;
    }
    if (char !== "(") continue;
    let end = index;
    while (end > 0 && CSS_WHITESPACE.test(text.charAt(end - 1))) end -= 1;
    let start = end;
    while (start > 0 && FUNCTION_NAME_CHAR.test(text.charAt(start - 1))) start -= 1;
    const name = text.slice(start, end);
    if (!NAMES_A_FUNCTION.test(name)) {
      open.push("");
      continue;
    }
    if (end !== index) return false;
    const allowed =
      SAFE_THEME_FUNCTIONS.has(name) || (WEBKIT_GRADIENT_ARGUMENTS.has(name) && open.includes("-webkit-gradient"));
    if (!allowed) return false;
    open.push(name);
  }
  return quote === null && open.length === 0;
}

/** `value` without the trim characters at either end: what is judged, and what the preview serves. */
export function trimThemeValue(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && TRIM_CHARS.has(value.charAt(start))) start += 1;
  while (end > start && TRIM_CHARS.has(value.charAt(end - 1))) end -= 1;
  return value.slice(start, end);
}

/** Whether `value` may be painted for theme key `key` (see the module comment). */
export function isSafeThemeValue(key: string, value: unknown): boolean {
  if (typeof value !== "string") return false;
  const text = trimThemeValue(value);
  if (!text) return false;
  if (THEME_FILE_ID_KEYS.includes(key)) return UUID.test(text);
  if (text.length > MAX_THEME_VALUE_CHARS) return false;
  return callsOnlySafeFunctions(decodeCssEscapes(text).toLowerCase()) && !DISALLOWED_CHAR.test(text);
}
