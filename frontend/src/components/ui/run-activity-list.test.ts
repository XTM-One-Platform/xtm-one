/**
 * The runs Activity tabs keep one scrollbar and one control height (#9378 slice 4a,
 * UX round 1 B1 and B2 on #11934) - `yarn test`.
 * - The grid view of RunActivityList passes `fitGrid`, so the run cards scroll in the
 *   table's fit-to-window area, as Executions, never the whole document.
 * - The agent tab's Delete All sits at the 32 px toolbar height (h-8), never h-9.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as list-toolbar-compact.test.ts.
const SRC = join(process.cwd(), "src");

test("the runs Activity grid is the table's fit-to-window scroll area", () => {
  const source = readFileSync(join(SRC, "components", "ui", "run-activity-list.tsx"), "utf8");
  const table = source.match(/<DataTable\b([\s\S]*?)renderGrid=/);
  assert.ok(table, "RunActivityList renders no DataTable with renderGrid");
  assert.match(table[1], /^\s*fitGrid\s*$/m);
});

test("the agent Activity Delete All button has the toolbar control height", () => {
  const source = readFileSync(join(SRC, "pages", "agent-detail", "ActivityTab.tsx"), "utf8");
  const button = source.match(/<Button\b([^>]*?)data-testid="run-delete-all"/);
  assert.ok(button, "the agent Activity tab renders no Delete All button");
  assert.match(button[1], /className="h-8\b/);
  assert.doesNotMatch(button[1], /\bh-9\b/);
});
