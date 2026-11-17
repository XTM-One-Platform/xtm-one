/**
 * Which toasts are on screen once one more is shown - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { showToast } from "./toast-list.ts";

interface Shown {
  id: string;
  title: string;
  tag?: string;
}

const offer = (id: string, title = "Reload to play"): Shown => ({ id, title, tag: "arcade-reload" });

const saved: Shown = { id: "toast-1", title: "Saved" };

test("an untagged toast is added after the others, twins included", () => {
  const twin: Shown = { id: "toast-2", title: "Saved" };
  assert.deepEqual(showToast([saved], twin), [saved, twin]);
});

test("the reload offer asked for on every Play click is on screen once, the latest copy", () => {
  let showing: Shown[] = [saved];
  for (let click = 2; click <= 6; click += 1) showing = showToast(showing, offer(`toast-${click}`));
  assert.deepEqual(showing, [saved, offer("toast-6")]);
});

test("the latest request wins: the leaderboard's offer replaces the game's", () => {
  const showing = showToast([offer("toast-1")], offer("toast-2", "Reload to open the leaderboard"));
  assert.deepEqual(showing, [offer("toast-2", "Reload to open the leaderboard")]);
});

test("another tag stays, and the input list is left as it was", () => {
  const other: Shown = { id: "toast-1", title: "Uploaded", tag: "upload" };
  const before: Shown[] = [other, offer("toast-2")];
  const after = showToast(before, offer("toast-3"));
  assert.deepEqual(after, [other, offer("toast-3")]);
  assert.deepEqual(before, [other, offer("toast-2")]);
});

test("the provider shows every toast through it, and the arcade's two offers share one tag", () => {
  const provider = readFileSync(new URL("./toast.tsx", import.meta.url), "utf8");
  assert.match(provider, /setToasts\(\(prev\) => showToast\(prev, \{ id, \.\.\.opts \}\)\)/);
  const arcade = readFileSync(new URL("../arcade/ArcadeProvider.tsx", import.meta.url), "utf8");
  assert.match(arcade, /const RELOAD_OFFER_TAG = "arcade-reload";/);
  const offerReload = arcade.match(/const offerReload = useCallback\([\s\S]*?\n {2}\);/)?.[0] ?? "";
  assert.match(offerReload, /tag: RELOAD_OFFER_TAG,/);
  // Still a click, never an automatic reload.
  assert.match(offerReload, /action: \{ label: t_i18n\("Reload"\), onClick: \(\) => reloadInto\(target\) \}/);
  assert.equal(arcade.match(/reloadInto\(/g)?.length, 2, "reloadInto is defined once and called from the button only");
});
