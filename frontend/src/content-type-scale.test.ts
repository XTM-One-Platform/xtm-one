/**
 * Gate: every `content-*` class a component writes is a real class.
 *
 * The design-system type scale is a family of `content-*` utilities
 * (`content-base`, `content-compact`, `content-caption`, ...). A name outside
 * that family (`content-small`) compiles to nothing: Tailwind emits no rule,
 * the element silently inherits the parent's size and no other check notices
 * (UX round 3 of #10181 found a binding line rendered at the wrong size this
 * way). The type scale is read from the safelist in `src/index.css`, which
 * names every `content-*` class the build emits; Tailwind's own `align-content`
 * utilities (`content-center`, `content-start`, ...) are the only other ones.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as focusable-badge-trigger.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** Tailwind core `align-content` utilities. */
const ALIGN_CONTENT = ["normal", "center", "start", "end", "between", "around", "evenly", "baseline", "stretch"];

/** The `content-*` type-scale classes the safelist of index.css declares. */
function typeScale(): Set<string> {
  const css = readFileSync(join(SRC, "index.css"), "utf8");
  const names = new Set<string>();
  for (const line of css.split("\n").filter((l) => l.startsWith("@source inline("))) {
    for (const hit of line.matchAll(/(?<=[\s"(])content-[a-z0-9-]+(?=[\s")])/g)) names.add(hit[0]);
  }
  return names;
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : [];
  });
}

/** A class list: a one-line string literal of at least two space-separated tokens. */
const CLASS_LIST = /"([^"\n]*\s[^"\n]*)"|`([^`\n]*\s[^`\n]*)`/g;

test("the type scale is read from the safelist", () => {
  const scale = typeScale();
  expect(scale.has("content-compact")).toBe(true);
  expect(scale.has("content-caption")).toBe(true);
  expect(scale.has("content-small")).toBe(false);
});

test("no component writes an unknown content-* class", () => {
  const known = typeScale();
  for (const name of ALIGN_CONTENT) known.add(`content-${name}`);
  const offenders: string[] = [];
  for (const path of sourceFiles(SRC)) {
    const text = readFileSync(path, "utf8");
    for (const literal of text.matchAll(CLASS_LIST)) {
      const body = literal[1] ?? literal[2] ?? "";
      for (const token of body.split(/\s+/)) {
        // A variant prefix (`md:`, `dark:`) is not part of the class name.
        const name = token.slice(token.lastIndexOf(":") + 1);
        if (/^content-[a-z0-9-]+$/.test(name) && !known.has(name)) {
          const line = text.slice(0, literal.index).split("\n").length;
          offenders.push(`${path.slice(SRC.length)}:${line} ${name}`);
        }
      }
    }
  }
  expect(offenders).toEqual([]);
});
