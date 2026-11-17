/**
 * Guard for the agent's built-in tools edit grid - `yarn test:unit`.
 *
 * The card name is cut with an ellipsis on a narrow card (1024 px, three
 * columns, long fr and es group names such as "Renseignement sur les
 * menaces"). Like the General Assistant grid (ResourceToggleCard), every
 * card exposes its full name, then its description, in a tooltip on the
 * button, available or not.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./ToolingSection.tsx", import.meta.url), "utf8");

test("every built-in card of the edit grid carries a tooltip with its full name and description", () => {
  const card = source.indexOf("data-testid={`builtin-tool-${tool.id}`}");
  assert.ok(card > 0, "the edit grid card button is found");
  const tipStart = source.lastIndexOf("<Tip content={", card);
  assert.ok(tipStart > 0, "the card button sits in a Tip");
  const tip = source.slice(tipStart, card);
  assert.doesNotMatch(tip, /isUnavailable \?[^:]*: undefined/, "the tooltip is not limited to an unavailable card");
  assert.match(tip, /builtin-tool-tip-\$\{tool\.id\}/);
  assert.match(tip, /\{tool\.label\}/, "the tooltip leads with the full name");
  assert.match(tip, /capTipText\(tool\.description\)/, "the tooltip carries the description");
});
