/**
 * Nothing under a records list (#11905, regression of #11836) - `yarn test`.
 *
 * The records table fits the window with its own scroller only while it is the
 * page's last block (`useFitToViewport`, `isLastBlock`). A line drawn under it
 * (the built-in fold row "N built-in agents hidden · Show") made it a widget: the
 * inner scroller was dropped and the whole page scrolled, on the default landing
 * (Company, built-ins hidden). The built-in toggle lives in the Properties filter
 * (Agents, Fleets) and in the Filters panel (the Agentic Flow map); Company's
 * tooltip counts what it hides.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as fleet-page-tabs.test.ts.
const read = (...parts: string[]) => readFileSync(join(process.cwd(), "src", ...parts), "utf8");
const AGENTS = read("pages", "Agents.tsx");
const FLEETS = read("pages", "flows", "FleetsTab.tsx");
const MAP = read("pages", "flows", "MapTab.tsx");

/** The JSX after `marker`'s element closes, comments and whitespace left out. */
function after(source: string, marker: string, closing: string): string {
  const start = source.lastIndexOf(marker);
  assert.ok(start >= 0, `${marker} not found`);
  const end = source.indexOf(closing, start);
  assert.ok(end >= 0, `${closing} not found after ${marker}`);
  return source
    .slice(end + closing.length)
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/\s+/g, "");
}

test("the Agents records table is the page's last block, so it keeps its own scroller", () => {
  // The EntityTable closes, then only the closing tags of the page.
  const tail = after(AGENTS, "<EntityTable", "\n            />\n");
  assert.equal(tail, ")}</></div>);}", `something is rendered after the Agents table: ${tail}`);
});

test("the Fleets grid and its pager are the page's last block: no line under them", () => {
  // After the pager, the list closes, then only the dialogs (portals, nothing drawn in the page flow).
  const tail = after(FLEETS, "<Pagination", "/>\n");
  assert.ok(tail.startsWith("</div>)}{dialog}<FleetBulkDialog"), `something is rendered after the Fleets list: ${tail.slice(0, 120)}`);
  // "Not in a fleet: N agents" sits in the toolbar row, before the count.
  assert.ok(FLEETS.indexOf("<NotInAFleetLine") < FLEETS.indexOf("<FleetEntryRow"), "NotInAFleetLine left the toolbar row");
});

test("no page draws the built-in fold row under its list", () => {
  assert.equal(existsSync(join(process.cwd(), "src", "components", "ui", "builtin-fold-row.tsx")), false);
  for (const [name, source] of [["Agents", AGENTS], ["Fleets", FLEETS], ["Map", MAP]] as const) {
    assert.doesNotMatch(source, /BuiltinFoldRow|builtin-fold-row/, `${name} still renders the fold row`);
  }
});

test("the built-in toggle stays reachable in the toolbar of each page", () => {
  assert.match(AGENTS, /useBuiltinPropertyFilter\(/);
  assert.match(FLEETS, /useBuiltinPropertyFilter\(/);
  // The map has no Properties filter: the switch is in its Filters panel, on both views.
  assert.equal(MAP.match(/<AgentGraphFilterControls[^>]*builtin=\{builtinSwitch\}/g)?.length, 2);
  for (const source of [AGENTS, FLEETS, MAP]) assert.match(source, /useBuiltinCompanyTip\(/);
});
