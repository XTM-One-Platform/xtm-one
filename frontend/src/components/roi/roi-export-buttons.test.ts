/**
 * One ROI export control (#12077 L6a, UX round 1 B2) - `yarn test:unit`.
 * The Export CSV / Export PDF pair is `RoiExportButtons`, never a page-local copy:
 * no other source file calls `useExportRoiReport` or draws its own pair.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src");
const OWNER = "src/components/roi/RoiExportButtons.tsx";
const HOOK = "src/hooks/api/useRoiReportApi.ts";

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("RoiExportButtons is the only ROI export control", () => {
  it("is the only caller of useExportRoiReport", () => {
    const callers = sources(SRC)
      .filter((path) => readFileSync(path, "utf8").includes("useExportRoiReport("))
      .map((path) => relative(process.cwd(), path).split("\\").join("/"))
      .filter((path) => path !== HOOK);
    expect(callers).toEqual([OWNER]);
  });
});
