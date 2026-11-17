// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The PIR chips of Settings > Organization are the shared Badge (#9615, UX
 * round 1 B1): a page-local chip copied Badge's fill and drew its own edge at
 * 1.15:1 light / 2.26:1 dark, under the 3:1 of a non-text boundary that Badge
 * `outline` is guarded at (outline-badge-border-contrast.test.ts). Static,
 * like the other design-system guards: the source has no state it cannot see.
 */

const FILE = path.resolve(__dirname, "../src/pages/settings/OrganizationPirs.tsx");

describe("OrganizationPirs chips", () => {
  it("renders every chip with the shared Badge", async () => {
    const source = await readFile(FILE, "utf8");
    expect(source).toMatch(/import \{ Badge \} from "@\/components\/ui\/badge"/);
    expect(source).toMatch(/<Badge\s+variant=\{pir\.state === "missing" \? "outline" : "secondary"\}/);
  });

  it("draws no chip fill or edge of its own", async () => {
    const source = await readFile(FILE, "utf8");
    for (const local of ["bg-elevation-hover", "border-elevation-subtle", "rounded-sm border", "content-compact text-default-primary"]) {
      expect(source, `OrganizationPirs.tsx paints its own chip with "${local}": use Badge`).not.toContain(local);
    }
  });

  it("outlines the chip skeleton with the outline Badge border, never the tile's fill (UX #11595 R1 B1)", async () => {
    const source = await readFile(FILE, "utf8");
    expect(source).toMatch(/<Skeleton className="[^"]*\bborder border-feedback-neutral-primary bg-transparent\b[^"]*" \/>/);
  });
});
