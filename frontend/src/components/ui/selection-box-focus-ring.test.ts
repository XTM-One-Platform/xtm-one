/**
 * The shared SelectionBox draws the design-system focus ring - `yarn test`.
 *
 * Keyboard focus on an enabled SelectionBox painted the browser's default
 * outline in currentColor (3 px grey on light, 2 px on dark) instead of the
 * 2 px offset plus 2 px `ring-focus` the Button and the Tip wrapper draw.
 * The enabled box now carries
 * SELECTION_BOX_FOCUS_RING; this pins it, and pins the disabled ink of a
 * disabled catalog facet's count chip (A2), which kept the enabled ink.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as tip-wrap-focus-ring.test.ts.
const SRC = join(process.cwd(), "src");

test("the enabled SelectionBox carries the 2 px offset plus 2 px ring-focus ring", () => {
  const source = readFileSync(join(SRC, "components", "ui", "list-control-bar.tsx"), "utf8");
  const ring = source.match(/const SELECTION_BOX_FOCUS_RING =\s*"([^"]+)"/)?.[1] ?? "";
  for (const cls of [
    "focus-visible:outline-none",
    "focus-visible:ring-2",
    "focus-visible:ring-focus",
    "focus-visible:ring-offset-2",
    "focus-visible:ring-offset-focus",
  ]) {
    expect(ring.split(/\s+/)).toContain(cls);
  }
  // Applied once, on the enabled box, beside its border.
  expect(source.split("ENABLED_BOX_BORDER,\n        SELECTION_BOX_FOCUS_RING,").length - 1).toBe(1);
});

test("a disabled catalog facet's count chip takes the disabled ink", () => {
  // The facet row lives in the shared FacetSidebar since #1501 (the catalog and the CTEM asset list draw it).
  const source = readFileSync(join(SRC, "components", "ui", "facet-sidebar.tsx"), "utf8");
  expect(source).toContain('disabled ? "text-default-disabled" : "text-default-secondary",\n        )}\n      >\n        {count}');
});
