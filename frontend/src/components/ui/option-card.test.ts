/**
 * One radio card and one refusal box - `yarn test`.
 *
 * The radio card's classes live in `components/ui/option-card.tsx` alone and the
 * fleet dialogs' refusal box in `components/ui/refusal-alert.tsx` alone (UX round
 * 1 B3 and B4 on #9209: the fork dialog had copied both). A copy of either class
 * string anywhere else fails here: draw it from the shared component instead.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as filter-pill-class.test.ts.
const SRC = join(process.cwd(), "src") + sep;

const SHARED: { owner: string; fragment: string; use: string }[] = [
  {
    owner: "components/ui/option-card.tsx",
    fragment: "flex w-full items-start gap-3 rounded-sm border px-3 py-2.5 text-left",
    use: "OptionCard from components/ui/option-card",
  },
  {
    owner: "components/ui/refusal-alert.tsx",
    // Closed by its quote: the profile pages' error lines extend the same start
    // with their own colour (text-feedback-error-primary), a different box.
    fragment: '"rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 px-3 py-2 content-compact"',
    use: "RefusalAlert from components/ui/refusal-alert",
  },
];

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

const files = [...sourceFiles(SRC)].map((full) => ({
  path: full.slice(SRC.length).split(sep).join("/"),
  source: readFileSync(full, "utf8"),
}));

test.each(SHARED)("$owner is the only file that draws its class string", ({ owner, fragment, use }) => {
  const holders = files.filter((f) => f.source.includes(fragment)).map((f) => f.path);
  expect(holders).toContain(owner);
  expect(holders.filter((path) => path !== owner), `a page re-declares it: use ${use}`).toEqual([]);
});
