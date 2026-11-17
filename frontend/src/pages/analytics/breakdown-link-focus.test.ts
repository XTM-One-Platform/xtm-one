// The Cost breakdown card draws no link of its own in a cell (#12070): the row is the
// click and keyboard target (rowHref), and the shared table draws its focus ring.
// happy-dom paints no focus ring, so the guard reads the card's source.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(join(process.cwd(), "src/pages/analytics/BreakdownCard.tsx"), "utf8");

describe("Cost breakdown rows", () => {
  it("draws no link in a cell", () => {
    expect(source).not.toMatch(/<Link\b|<a\b/);
  });

  it("opens the agent, the user and the group from the row", () => {
    const rowHrefs = [...source.matchAll(/rowHref=\{/g)];
    expect(rowHrefs).toHaveLength(3);
  });
});
