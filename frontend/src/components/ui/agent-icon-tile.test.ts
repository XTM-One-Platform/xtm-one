/**
 * One agent icon tile on the home - `yarn test`. The Agents, Last
 * conversations and Latest results widgets and the Top value drivers card draw
 * an agent's icon through `AgentIconTile` (#12009, UX design round C2); a
 * hand-written accent tile in those files is a second implementation.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const FILES = ["src/pages/Dashboard.tsx", "src/components/roi/RoiValueDriversCard.tsx"];
const HAND_TILE = /rounded-sm bg-accent\/10 group-hover:bg-accent\/15/;

test("the home draws every agent icon tile through AgentIconTile", () => {
  for (const file of FILES) {
    const source = readFileSync(join(process.cwd(), file), "utf8");
    assert.doesNotMatch(source, HAND_TILE, `${file}: render <AgentIconTile> instead of a hand-written tile`);
    assert.match(source, /<AgentIconTile\b/, `${file}: no AgentIconTile`);
  }
});
