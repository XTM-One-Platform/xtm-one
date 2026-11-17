/**
 * Unit tests for the chunk-load helpers - `yarn test:unit`.
 */
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";

import { isChunkLoadError, warmChunksWhenIdle } from "./lazyWithRetry.ts";

const scope = globalThis as { window?: unknown };
const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

interface Connection {
  saveData?: boolean;
  effectiveType?: string;
}

/** A browser whose idle callbacks run when `idle()` says so. */
function fakeBrowser(connection?: Connection) {
  const pending = new Map<number, () => void>();
  let next = 0;
  scope.window = {
    requestIdleCallback: (task: () => void) => {
      pending.set(++next, task);
      return next;
    },
    cancelIdleCallback: (handle: number) => void pending.delete(handle),
  };
  Object.defineProperty(globalThis, "navigator", { value: { connection }, configurable: true, writable: true });
  return {
    idle: () => {
      for (const [handle, task] of [...pending]) {
        pending.delete(handle);
        task();
      }
    },
    scheduled: () => pending.size,
  };
}

/** Let every queued promise callback run, so a warm-up that should not have
 *  happened would have by now. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

afterEach(() => {
  delete scope.window;
  if (navigatorDescriptor) Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
  else delete (globalThis as { navigator?: unknown }).navigator;
});

test("a chunk that did not download is told apart from a module that crashed", () => {
  const url = "https://xtm-one.example.com/assets/ArcadeOverlay-AQxG0y25.js";
  assert.equal(isChunkLoadError(new TypeError(`Failed to fetch dynamically imported module: ${url}`)), true);
  assert.equal(isChunkLoadError(new TypeError(`error loading dynamically imported module: ${url}`)), true);
  assert.equal(isChunkLoadError(new TypeError("Importing a module script failed.")), true);
  assert.equal(isChunkLoadError(new Error("Unable to preload CSS for /assets/index.css")), true);
  assert.equal(isChunkLoadError(new TypeError("Cannot read properties of undefined (reading 'score')")), false);
  assert.equal(isChunkLoadError(new TypeError("Failed to fetch")), false, "an API call that failed");
});

test("the warm-up imports every chunk once the browser is idle, one at a time, past a failure", async () => {
  const browser = fakeBrowser();
  const started: string[] = [];
  // Settled by the test: the order is read off the imports, never off a clock.
  let failOverlay: (error: Error) => void = () => {};
  const overlay = new Promise<void>((_, reject) => {
    failOverlay = reject;
  });
  let leaderboardStarted: () => void = () => {};
  const leaderboard = new Promise<void>((resolve) => {
    leaderboardStarted = resolve;
  });

  warmChunksWhenIdle([
    () => {
      started.push("overlay");
      return overlay;
    },
    async () => {
      started.push("leaderboard");
      leaderboardStarted();
    },
  ]);
  assert.deepEqual(started, [], "nothing competes with the first paint");

  browser.idle();
  assert.deepEqual(started, ["overlay"], "the next import waits for this one");
  failOverlay(new TypeError("Failed to fetch dynamically imported module"));
  await leaderboard;
  assert.deepEqual(started, ["overlay", "leaderboard"]);
});

test("a warm-up cancelled before the browser is idle imports nothing", async () => {
  const browser = fakeBrowser();
  let imports = 0;
  const cancel = warmChunksWhenIdle([async () => void (imports += 1)]);
  cancel();
  assert.equal(browser.scheduled(), 0);
  browser.idle();
  await flush();
  assert.equal(imports, 0);
});

test("a warm-up cancelled while a chunk downloads starts no other", async () => {
  const browser = fakeBrowser();
  const started: string[] = [];
  let finishOverlay: () => void = () => {};
  const overlay = new Promise<void>((resolve) => {
    finishOverlay = resolve;
  });
  const cancel = warmChunksWhenIdle([
    () => {
      started.push("overlay");
      return overlay;
    },
    async () => void started.push("leaderboard"),
  ]);

  browser.idle();
  assert.deepEqual(started, ["overlay"]);
  cancel();
  finishOverlay();
  await overlay;
  await flush();
  assert.deepEqual(started, ["overlay"], "a sign-out mid warm-up starts nothing more");
});

test("no warm-up on Data Saver or a 2G connection", async () => {
  for (const connection of [{ saveData: true }, { effectiveType: "2g" }, { effectiveType: "slow-2g" }]) {
    const browser = fakeBrowser(connection);
    let imports = 0;
    warmChunksWhenIdle([async () => void (imports += 1)]);
    assert.equal(browser.scheduled(), 0, JSON.stringify(connection));
    browser.idle();
    await flush();
    assert.equal(imports, 0, JSON.stringify(connection));
  }
});

test("without requestIdleCallback the warm-up waits a fixed delay instead", async () => {
  const delays: number[] = [];
  scope.window = {
    setTimeout: (task: () => void, delay: number) => {
      delays.push(delay);
      task();
      return 1;
    },
    clearTimeout: () => undefined,
  };
  Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true, writable: true });
  let imports = 0;
  warmChunksWhenIdle([async () => void (imports += 1)]);
  await flush();
  assert.deepEqual(delays, [1500]);
  assert.equal(imports, 1);
});
