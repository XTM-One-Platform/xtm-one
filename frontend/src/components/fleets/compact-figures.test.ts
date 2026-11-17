// Compact figures on the fleet About tiles and the Home cards (#10476): an ICU
// `{x, number}` argument draws "1,238" in full, so these surfaces pass the
// compact text as a plain argument and keep the exact figure for the hover.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..", "..");
const FLEETS = join(ROOT, "components", "fleets");
const SOURCES = [
  ...readdirSync(FLEETS)
    .filter((name) => name.startsWith("FleetAbout") && name.endsWith(".tsx") && !name.includes(".test."))
    .map((name) => join(FLEETS, name)),
  join(ROOT, "pages", "Dashboard.tsx"),
];

describe("compact figures on the fleet About tiles and the Home cards (#10476)", () => {
  it("never formats a figure through an ICU number argument", () => {
    const offenders = SOURCES.filter((path) => /t_i18n\(\s*"[^"]*\{\w+, number\}/.test(readFileSync(path, "utf8")));
    expect(offenders.map((path) => path.slice(ROOT.length + 1))).toEqual([]);
  });

  it("gives the quota estimate tile its exact figure on hover", () => {
    const source = readFileSync(join(FLEETS, "FleetAboutShowcase.tsx"), "utf8");
    expect(source).toContain("exact={quotaExact}");
    expect(source).toContain("<CompactFigure exact={quotaExact}>");
  });

  it("gives every Home KPI card its exact count on hover", () => {
    const source = readFileSync(join(ROOT, "pages", "Dashboard.tsx"), "utf8");
    expect(source).toContain("title={exactMetricValue(value)}");
  });
});
