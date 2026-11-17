import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Compact numbers sweep (#10476): a count drawn in a card title badge goes
// through KpiCount (compact, exact value on hover), never a raw `{expr}`.
describe("AI adoption count badges", () => {
  // vitest runs from frontend/ (CI and `yarn test`).
  const DIR = join(process.cwd(), "src/pages/ai-adoption");
  // A translated label (`{t_i18n("Connected")}`) is text, not a figure.
  const RAW_BADGE_COUNT = /<Badge\b[^>]*>\s*\{(?!t_i18n\()[^{}<>]+\}\s*<\/Badge>/g;

  it("draw every count badge through KpiCount, never a raw figure", () => {
    const offenders = readdirSync(DIR)
      .filter((file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx"))
      .flatMap((file) => (readFileSync(join(DIR, file), "utf8").match(RAW_BADGE_COUNT) ?? []).map((hit) => `${file}: ${hit}`));
    expect(offenders).toEqual([]);
  });

  // A chart axis tick prints a figure through formatCompactNumber (or a date
  // through fmtChartDate), never a raw `{val}` such as "5224".
  const RAW_TICK = /<text\b[^>]*>\s*\{(?!formatCompactNumber\(|fmtChartDate\()[^{}<>]+\}\s*<\/text>/g;

  it("draw every chart axis tick compact, never a raw figure", () => {
    const offenders = readdirSync(DIR)
      .filter((file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx"))
      .flatMap((file) => (readFileSync(join(DIR, file), "utf8").match(RAW_TICK) ?? []).map((hit) => `${file}: ${hit}`));
    expect(offenders).toEqual([]);
  });

  it("draw both Gaps user tables' badges compact", () => {
    const gaps = readFileSync(join(DIR, "GapsTab.tsx"), "utf8");
    expect(gaps.match(/<KpiCount value=\{users\.length\} \/><\/Badge>/g)?.length).toBe(2);
  });
});
