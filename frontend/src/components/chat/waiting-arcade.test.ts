/**
 * One launch path for the waiting arcade's offers - `yarn test:unit`.
 *
 * The waiting panel's Take control offers the arcade while an agent works,
 * through `useWaitingArcade`, which reads the arcade's gate and reduced motion
 * once and launches with the chat surface's own `waitSource`. There is no
 * per-user switch to hide the game, and the gamepad icon means play.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const HOOK = read("./waiting-arcade.ts");
const PANEL = read("./ChatWaitingGame.tsx");
const REASONING = read("./ReasoningArcadeAction.tsx");
const HOST = read("../chat-panel/XtmChatPanel.tsx");

test("the game is offered through the arcade's gate and reduced motion, and waits on the surface", () => {
  assert.match(HOOK, /const playMode = !reducedMotion;/);
  assert.match(HOOK, /const canTakeControl = playMode && arcade\.enabled;/);
  assert.match(HOOK, /if \(!canTakeControl\) return;\s*arcade\.launch\(\{ source: "waiting", origin, waitSource \}\);/);
});

test("the waiting panel and the reasoning window launch through it, and nothing else in the chat does", () => {
  assert.match(PANEL, /useWaitingArcade\(\{\s*waitSource\s*\}\)/);
  assert.match(REASONING, /useWaitingArcade\(\{\s*waitSource\s*\}\)/);
  for (const [name, source] of [
    ["ChatWaitingGame.tsx", PANEL],
    ["ReasoningArcadeAction.tsx", REASONING],
    ["XtmChatPanel.tsx", HOST],
  ]) {
    assert.doesNotMatch(source, /\.launch\(/, `${name} launches the arcade itself: go through useWaitingArcade`);
  }
  assert.doesNotMatch(
    PANEL,
    /usePrefersReducedMotion|\barcade\.enabled\b|useArcade\(\)\.enabled/,
    "ChatWaitingGame reads an input of the offer: take it from useWaitingArcade",
  );
});

test("the panel's waiting game waits on the one wait signal of the chat surface", () => {
  assert.equal(HOST.match(/useArcadeWaitSignal\(/g)?.length, 1);
  assert.match(HOST, /useArcadeWaitSignal\(conversationId, working\)/);
  assert.match(HOST, /<ChatWaitingGame waitSource=\{arcadeWaitSource\} \/>/);
});

test("there is no switch to hide the game: no toggle, no stored preference read", () => {
  assert.equal(existsSync(new URL("./waiting-game-preference.ts", import.meta.url)), false);
  const src = fileURLToPath(new URL("../../", import.meta.url));
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.tsx?$/.test(name) && !name.endsWith(".test.ts")) files.push(path);
    }
  };
  walk(src);
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    assert.doesNotMatch(text, /waiting-game-preference|WaitingGamePreference|minigameOn/, `${file} still reads the removed switch`);
    // The old key is only ever dropped, once, by the hook.
    if (!file.endsWith("waiting-arcade.ts")) assert.doesNotMatch(text, /filigranChatMiniGame/, file);
  }
  assert.match(HOOK, /removeItem\("filigranChatMiniGame"\)/);
  assert.doesNotMatch(PANEL, /Turn (off|on) the waiting mini-game/);
  for (const lang of ["en", "es", "fr", "ja"]) {
    assert.doesNotMatch(read(`../../i18n/lang/${lang}.json`), /waiting mini-game/, `${lang}.json keeps a removed string`);
  }
});

test("the gamepad means play: every launch affordance of the chat shows it, and nothing else does", () => {
  const takeControl = PANEL.match(/aria-label=\{t_i18n\("Take control of the waiting game"\)\}[\s\S]*?<\/button>/)?.[0] ?? "";
  assert.match(takeControl, /<Icon name="gamepad-2"/);
  assert.equal(PANEL.match(/<Icon name="gamepad-2"/g)?.length, 1);
  assert.doesNotMatch(PANEL, /<Play /);
  assert.equal(REASONING.match(/<Icon name="gamepad-2"/g)?.length, 1);
});
