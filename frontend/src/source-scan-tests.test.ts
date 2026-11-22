/**
 * The source-scan guards a pull request runs (`scripts/source-scan-tests.mjs`):
 * a change breaks one without naming its files, so CI's "Frontend gates" job
 * runs each of them on every pull request, not only the merge group (#745).
 */
import { describe, expect, it } from "vitest";
import { scansTheTree, sourceScanTests } from "../scripts/source-scan-tests.mjs";

describe("source-scan tests", () => {
  const found = sourceScanTests(process.cwd());

  it("lists the guards that walk the tree", () => {
    expect(found).toContain("src/lib/mode-split-guard.test.ts");
    expect(found).toContain("src/components/ui/data-table-gates.test.ts");
  });

  it("lists only vitest files of src/ and tests_unit/", () => {
    expect(found.every((path) => /^(src|tests_unit)\/.+\.test\.tsx?$/.test(path))).toBe(true);
  });

  it("needs both an fs import and a directory listing", () => {
    expect(scansTheTree('import { readdirSync } from "node:fs";\nreaddirSync(dir);')).toBe(true);
    expect(scansTheTree('import { readFileSync } from "node:fs";\nreadFileSync(file);')).toBe(false);
    expect(scansTheTree("const names = readdirSync(dir);")).toBe(false);
  });
});
