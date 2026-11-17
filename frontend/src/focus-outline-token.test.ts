/**
 * No native focus outline anywhere in the app - `yarn test`.
 *
 * Tailwind v4 preflight does not reset `outline`, so the browser's own rule
 * `:focus-visible { outline: auto 1px -webkit-focus-ring-color }` painted an
 * outline in the OS accent colour (orange on a Mac set to orange) on every
 * hand-written control that did not reset it, alone or on top of our 2 px
 * `ring-focus` (issue #11542). Two guards keep it from coming back:
 *
 * 1. `index.css` replaces the browser outline on `:focus-visible` with the
 *    focus token in the base layer, so a control with no style of its own
 *    still gets our indicator, never the native one.
 * 2. A class list that draws the focus ring as a box-shadow (`ring-*` under a
 *    focus variant) also resets `outline`, so the base outline does not draw
 *    a second ring around it.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src") + sep;

function sources(dir: string, out: { path: string; text: string }[] = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") sources(full, out);
    } else if (/\.(css|tsx?)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push({ path: full.slice(SRC.length), text: readFileSync(full, "utf8") });
    }
  }
  return out;
}

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

test("index.css replaces the native focus outline with the focus token", () => {
  const css = stripComments(readFileSync(join(SRC, "index.css"), "utf8"));
  const base = css.match(/@layer base\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";
  const rule = base.match(/(^|\n)\s*:focus-visible\s*\{([^}]*)\}/)?.[2] ?? "";
  expect(rule, "a `:focus-visible` rule in the base layer of index.css").not.toBe("");
  expect(rule).toMatch(/outline:\s*2px solid var\(--color-filigran-brand-primary\)/);
  expect(rule).toMatch(/outline-offset:\s*2px/);
});

test("no stylesheet brings the native focus outline back", () => {
  const offenders = sources(SRC)
    .filter((f) => f.path.endsWith(".css"))
    .filter((f) => /outline(-style)?:\s*auto\b|-webkit-focus-ring-color/.test(stripComments(f.text)))
    .map((f) => f.path);
  expect(offenders).toEqual([]);
});

// A focus ring drawn as a box-shadow on the focused element itself:
// `focus-visible:ring-2`, `focus:ring-2`; not `ring-offset-*`, `ring-inset` or
// `ring-0`. A `focus-within:` or `has-[...:focus-visible]:` ring sits on a
// wrapper that never takes focus: the outline belongs to the focused child.
const RING = /(?<![\w-])(?:focus-visible|focus):ring-(?!offset|inset|0\b)/;
const RESET = /outline-(?:none|hidden|0)\b/;
const LITERAL = /"[^"\n]*"|`[^`]*`|'[^'\n]*'/g;

test("the pattern of the ring guard matches what it means to catch", () => {
  expect(RING.test("focus-visible:ring-2 focus-visible:ring-focus")).toBe(true);
  expect(RING.test("focus:ring-2")).toBe(true);
  expect(RING.test("has-[[data-card-link]:focus-visible]:ring-2")).toBe(false);
  expect(RING.test("focus-within:ring-2")).toBe(false);
  expect(RING.test("focus-visible:ring-offset-2")).toBe(false);
  expect(RESET.test("focus-visible:outline-hidden")).toBe(true);
});

test("every class list that draws a focus ring also resets the outline", () => {
  const offenders: string[] = [];
  for (const file of sources(SRC)) {
    if (!/\.tsx?$/.test(file.path)) continue;
    const text = file.text;
    for (const m of text.matchAll(LITERAL)) {
      const literal = m[0];
      if (!RING.test(literal) || RESET.test(literal)) continue;
      // A doc comment that spells the classes is not a class list.
      const lineStart = text.lastIndexOf("\n", m.index) + 1;
      if (/^\s*(\*|\/\/)/.test(text.slice(lineStart, m.index))) continue;
      // The reset may sit in a sibling literal of the same `cn(...)` call.
      const call = text.lastIndexOf("cn(", m.index);
      const end = text.indexOf(")", m.index + literal.length);
      if (call !== -1 && m.index - call < 600 && RESET.test(text.slice(call, end === -1 ? undefined : end))) continue;
      offenders.push(`${file.path}:${text.slice(0, m.index).split("\n").length}`);
    }
  }
  expect(offenders).toEqual([]);
});
