/**
 * One menu component in the frontend - `yarn test`.
 *
 * Every menu opened at a point (a right click on a graph node, on the flow
 * builder's canvas, node or edge) or from a row kebab is the design system's
 * `Menu`, through `PointMenu` (components/ui/point-menu.tsx) or `OverflowMenu`,
 * with rows from `OverflowMenuItems` (standing UI rule of the
 * CEO Office Developer skill, section 13). The hand-rolled `CanvasContextMenu`
 * (components/ui/context-menu.tsx: own surface, 12px rows, no focus on open,
 * no arrow keys) was deleted and must not come back anywhere under
 * src/. This guard used to cover components/fleets only
 * (fleet-member-actions.test.ts); it covers all of src/.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const SRC = join(process.cwd(), "src");
const SELF = join(SRC, "no-hand-rolled-menu.test.ts");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.(ts|tsx)$/.test(entry.name) && full !== SELF) {
      yield full;
    }
  }
}

const read = (path: string) => readFileSync(join(SRC, path), "utf8");

test("the hand-rolled canvas menu is gone", () => {
  assert.equal(existsSync(join(SRC, "components/ui/context-menu.tsx")), false);
});

test("no file under src/ builds its own menu: the design system's menu only", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    if (/CanvasContextMenu|components\/ui\/context-menu/.test(readFileSync(file, "utf8"))) {
      offenders.push(relative(SRC, file).split(sep).join("/"));
    }
  }
  assert.deepEqual(offenders, [], `hand-rolled menu in: ${offenders.join(", ")}`);
});

test("the point menus and the row kebab are the design system's", () => {
  assert.match(read("components/fleets/FleetNodeMenu.tsx"), /<PointMenu\b/);
  assert.match(read("pages/flow-builder/FlowCanvas.tsx"), /<PointMenu\b/);
  assert.match(read("components/fleets/FleetMembersPanel.tsx"), /<OverflowMenu\b/);
});
