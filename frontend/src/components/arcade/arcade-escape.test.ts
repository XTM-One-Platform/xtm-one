/**
 * What Escape does in the waiting arcade - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { escapeAction } from "./arcade-escape.ts";

test("the first Escape pauses, the second leaves: the game ends and its run is saved", () => {
  assert.equal(escapeAction("playing", false), "pause");
  assert.equal(escapeAction("paused", false), "quit");
});

test("once the answer is ready, Escape goes to it, paused or not", () => {
  assert.equal(escapeAction("playing", true), "answer");
  assert.equal(escapeAction("paused", true), "answer");
});

test("before the game starts and once it is over, Escape leaves at once", () => {
  for (const answerReady of [false, true]) {
    assert.equal(escapeAction("intro", answerReady), "leave");
    assert.equal(escapeAction("over", answerReady), "leave");
  }
});

test("nothing while the interface is being repaired", () => {
  assert.equal(escapeAction("leaving", false), null);
  assert.equal(escapeAction("leaving", true), null);
});

test("the HUD and the hint say what Escape does: it pauses, and a second press leaves", () => {
  const overlay = readFileSync(new URL("./ArcadeOverlay.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(overlay, /"Leave the arcade \(Esc\)"/);
  assert.doesNotMatch(overlay, /Esc to leave"/);
  assert.equal(overlay.match(/t_i18n\("Leave the arcade \(Esc twice\)"\)/g)?.length, 2);
  assert.match(overlay, /t_i18n\("[^"]*P or Esc to pause - Esc again to leave"\)/);
});

test("the overlay reads Escape from this table and ends the game the way its Leave button does", () => {
  const overlay = readFileSync(new URL("./ArcadeOverlay.tsx", import.meta.url), "utf8");
  assert.match(overlay, /const action = escapeAction\(current, answerReadyRef\.current\);/);
  // One end of a game however it is asked for - Escape, the HUD's Leave, the
  // pause menu's End: the run is submitted and the interface repaired once.
  assert.match(overlay, /action === "quit"\) quitGame\(\);/);
  assert.equal(overlay.match(/onClick=\{quitGame\}/g)?.length, 2);
  assert.equal(overlay.match(/\bgame\.quit\(\)/g)?.length, 1);
});
