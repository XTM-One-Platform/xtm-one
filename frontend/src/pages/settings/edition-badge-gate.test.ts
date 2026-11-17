// Never a licence-state badge drawn by hand (#9448, design
// dev-docs/roi-outcomes.md 6.1 C1): "Licensed" and "Community" inside a
// `Badge` of QuotasSection or ParametersSection go through `<EditionBadge />`.
// Not in scope: MCPEndpointSection's "Community" (a feature-tier marker, D1)
// and ParametersSection's "Trial" / "Active" / "Not Activated" (the licence
// activation status, E3), which are not edition badges.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// vitest runs from frontend/ (CI and `yarn test`).
const SETTINGS = join(process.cwd(), "src", "pages", "settings");
const SCOPED = ["QuotasSection.tsx", "ParametersSection.tsx"];
const HAND_DRAWN = /<Badge\b[^>]*>(?:(?!<\/Badge>)[\s\S])*t_i18n\("(Licensed|Community)"\)(?:(?!<\/Badge>)[\s\S])*<\/Badge>/g;

describe("licence-state badges", () => {
  for (const file of SCOPED) {
    it(`${file} draws no Licensed / Community badge by hand`, () => {
      const source = readFileSync(join(SETTINGS, file), "utf8");
      expect(source.match(HAND_DRAWN) ?? []).toEqual([]);
    });
  }

  it("catches a hand-drawn badge", () => {
    const sample = '<Badge variant="cyan">\n  <Icon /> {t_i18n("Licensed")}\n</Badge>';
    expect(sample.match(HAND_DRAWN)).toHaveLength(1);
  });
});
