/**
 * The pane's agent icons are a copy of the web app's `ICON_MAP`
 * (frontend/src/lib/constants.ts): a name missing here draws the chat panel's
 * generic glyph in the add-in while the web chat draws the agent's own icon.
 * The map's names are read from the web source so the two lists stay equal.
 */
import { expect, test } from "vitest";

import webConstants from "../../../../frontend/src/lib/constants.ts?raw";
import { AGENT_ICON_NAMES } from "./agentIcons";

function webIconNames(source: string): string[] {
  const start = source.indexOf("export const ICON_MAP");
  expect(start, "ICON_MAP in frontend/src/lib/constants.ts").toBeGreaterThan(-1);
  const body = source.slice(source.indexOf("{", start) + 1, source.indexOf("};", start));
  return body
    .split("\n")
    .map((line) => line.replace(/\/\/.*$/, "").trim())
    .filter(Boolean)
    .map((line) => line.replace(/,$/, "").split(":")[0].trim());
}

test("the add-in draws every agent icon the web ICON_MAP draws, and no other", () => {
  const web = webIconNames(webConstants);
  expect(web.length).toBeGreaterThan(50);
  expect([...AGENT_ICON_NAMES].sort()).toEqual([...web].sort());
});
