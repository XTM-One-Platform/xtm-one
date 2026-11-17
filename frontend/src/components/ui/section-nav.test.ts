/**
 * One section navigation row in the frontend (#9173, UX B3 and C2) - `yarn test`.
 *
 * A row of section pills under a page tab is `SectionNav`
 * (components/ui/section-nav.tsx). The gate targets navigation rows only: a
 * `<nav>` on the pill surface, or the hand-written row class the Security,
 * Integrations and agent Config rows used. The toggle and radio groups that
 * share the surface (role="group" / role="radiogroup": the security audit
 * views and grouping, the vault audience) are filters, not navigation, and
 * are out of scope (advisory A8).
 */
import { expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

const SRC = join(process.cwd(), "src");
const OWNER = join(SRC, "components", "ui", "section-nav.tsx");
const SELF = join(SRC, "components", "ui", "section-nav.test.ts");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx$/.test(entry.name) && full !== OWNER && full !== SELF) {
      yield full;
    }
  }
}

// The pill surface, `p-1` as a whole class (not `p-1.5`).
const SURFACE = /rounded-sm bg-elevation-hover p-1(?![.\w-])/;
const NAV_ROW = /<nav\b|"flex items-center gap-1 rounded-sm bg-elevation-hover p-1"/;

it("every section navigation row is SectionNav", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      if (SURFACE.test(line) && NAV_ROW.test(line) && !/role="(radio)?group"/.test(line)) {
        offenders.push(`${relative(SRC, file).split(sep).join("/")}:${i + 1}`);
      }
    });
  }
  expect(offenders).toEqual([]);
});

it("SectionNav is a nav with the current section marked", () => {
  const source = readFileSync(OWNER, "utf8");
  expect(source).toMatch(/<nav aria-label=\{t_i18n\(label\)\}/);
  expect(source).toMatch(/aria-current=\{isActive \? "page" : undefined\}/);
  expect(source).toMatch(/focus-visible:ring-2/);
});
