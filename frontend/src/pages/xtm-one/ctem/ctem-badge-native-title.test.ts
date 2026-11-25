/**
 * A CTEM badge never carries the browser's native `title` (UX round 1 B1 on
 * #2007): its reason is the design system tooltip (`Tip`), opened on hover and
 * on keyboard focus (dev-docs/reference/frontend-shared-utilities.md). The
 * cut-cell gate of data-table-gates only catches truncated cells, so a chip
 * with a `title=` prop slipped past it. The allowance below is the debt on main
 * when this guard landed: it may only shrink.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as chip-radius.test.ts.
const CTEM = join(process.cwd(), "src", "pages", "xtm-one", "ctem") + sep;

const BADGE_WITH_TITLE = /<(?:ToneBadge|Badge)\b[^>]*\stitle=/g;

/** File (relative to the CTEM folder) -> badges with a native title allowed. */
const ALLOWED: Record<string, number> = {
  "CaseOpenedBy.tsx": 1,
};

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : [];
  });
}

test("no CTEM badge carries a native title: the reason is the DS Tip", () => {
  const offenders: string[] = [];
  for (const path of sourceFiles(CTEM)) {
    const file = relative(CTEM, path).split(sep).join("/");
    const count = readFileSync(path, "utf8").match(BADGE_WITH_TITLE)?.length ?? 0;
    if (count > (ALLOWED[file] ?? 0)) offenders.push(`${file}: ${count} badge(s) with title=`);
  }
  expect(offenders).toEqual([]);
});
