/**
 * The detail tab bar fits a 1024 px viewport - `yarn test`.
 *
 * With the library's `px-4 gap-2` triggers
 * the six Operations tabs measured 802 px in a 748 px list at 1024 px, so the
 * strip scrolled and "Workspaces" hid behind the chevron. Every trigger of
 * `DetailTabs` now takes the tighter spacing below `xl` and the library's
 * spacing from `xl` up. The check is static, as `filter-pill-class.test.ts`:
 * a trigger rendered with another class brings the overflow back.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as filter-pill-class.test.ts.
const SOURCE = readFileSync(join(process.cwd(), "src", "components", "ui", "detail-tabs.tsx"), "utf8");

test("the trigger class is tight below xl and the library's spacing from xl", () => {
  const match = SOURCE.match(/const TAB_TRIGGER_CLASS = "([^"]+)";/);
  expect(match).not.toBeNull();
  const classes = match![1].split(/\s+/);
  expect(classes).toEqual(expect.arrayContaining(["whitespace-nowrap", "px-2.5", "gap-1.5", "xl:px-4", "xl:gap-2"]));
});

test("every TabsTrigger of DetailTabs uses the shared trigger class", () => {
  const triggers = SOURCE.match(/<TabsTrigger\b[^>]*>/g) ?? [];
  expect(triggers.length).toBeGreaterThan(0);
  for (const trigger of triggers) {
    expect(trigger).toContain("className={TAB_TRIGGER_CLASS}");
  }
});
