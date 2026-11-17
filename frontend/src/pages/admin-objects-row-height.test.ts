// Gate: every row of Administration > Objects keeps one fixed height
// (Design standards s.3; UX round 2 of #10442 B1:
// the archived fleet row was 75 px beside 72 px rows because its Archived
// badge was 24 px tall next to a 21 px type chip at 10 px).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "AdminObjects.tsx"), "utf8");

describe("Admin objects rows", () => {
  it("marks a company-managed row with the icon, not the 10 px shared badge", () => {
    // UX round 3 of #10442 B1: CompanyManagedBadge draws content-caption
    // (10 px) with a 10 px icon, and at 12 px it would make the row taller.
    expect(SOURCE).not.toMatch(/\bCompanyManagedBadge\b/);
    expect(SOURCE).toMatch(/obj\.is_company_managed && <CompanyManagedIcon \/>/);
  });

  it("draws the type chip at the Archived badge size, so an archived row is not taller", () => {
    // Since the shared data table (#9378) the label sits in a TruncatedText,
    // so a long type name truncates instead of widening the column.
    const typeChip = SOURCE.match(
      /<Badge variant="outline"( className="[^"]*")?>\s*(?:<TruncatedText text=)?\{config\?\.label/,
    );
    expect(typeChip).not.toBeNull();
    // The shared Badge is content-compact (12 px) by default: the type chip
    // overrides nothing smaller, as the Archived badge overrides nothing.
    expect(typeChip?.[1] ?? "").not.toMatch(/content-caption|text-\[|py-0\b|h-\d/);
    expect(SOURCE).toMatch(/<Badge variant="outline">\{t_i18n\("Archived"\)\}<\/Badge>/);
  });
});
