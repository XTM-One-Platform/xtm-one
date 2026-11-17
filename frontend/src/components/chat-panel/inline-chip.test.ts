/**
 * Gate: the compact answer chip has one look (#11697, UX round 2 B1). The
 * working-file chip and the inline marker chip read their classes from
 * `inline-chip.ts`; a copy of the chip surface anywhere else under src/ would
 * drift again (different hover, no focus ring).
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

const SRC = join(process.cwd(), "src") + sep;
const OWNER = join("components", "chat-panel", "inline-chip.ts");
/** The surface of the chip, the part a copy would carry. */
const SURFACE = "border-accent/25 bg-accent/10 px-3 py-1.5";

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

test("the chip surface is written once, in inline-chip.ts", () => {
  const holders = sourceFiles(SRC)
    .filter((path) => readFileSync(path, "utf8").includes(SURFACE))
    .map((path) => path.slice(SRC.length));
  expect(holders).toEqual([OWNER]);
});

test("both chips read the shared classes", () => {
  for (const file of ["FileChips.tsx", "MarkerInlineChip.tsx"]) {
    const text = readFileSync(join(SRC, "components", "chat-panel", file), "utf8");
    expect(text).toContain('from "./inline-chip"');
    expect(text).toContain("inlineChipClass(true)");
  }
});

test("an interactive chip shows the design system focus ring", () => {
  const text = readFileSync(join(SRC, OWNER), "utf8");
  expect(text).toContain("focus-visible:ring-2 focus-visible:ring-focus");
});
