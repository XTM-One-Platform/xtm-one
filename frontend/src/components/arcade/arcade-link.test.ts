/**
 * Unit tests for opening the waiting arcade from a link - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ARCADE_LINK_STORAGE_KEY,
  ARCADE_LINK_TTL_MS,
  type ArcadeLinkStorage,
  type ArcadeRequest,
  arcadeLinkVerdict,
  arcadeRequestTarget,
  arcadeRequestUrl,
  captureArcadeRequest,
  forgetArcadeRequest,
  isFreshArcadeRequest,
  requestsArcade,
  withArcadeRequest,
  withoutArcadeRequest,
} from "./arcade-link.ts";
import { arcadeGate, type ArcadeGateInputs } from "./arcade-gate.ts";

function memoryStorage(): ArcadeLinkStorage & { items: Map<string, string> } {
  const items = new Map<string, string>();
  return {
    items,
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}

const brokenStorage: ArcadeLinkStorage = {
  getItem: () => {
    throw new Error("storage disabled");
  },
  setItem: () => {
    throw new Error("storage disabled");
  },
  removeItem: () => {
    throw new Error("storage disabled");
  },
};

const game = (requestedAt: number): ArcadeRequest => ({ target: "game", requestedAt });
const leaderboard = (requestedAt: number): ArcadeRequest => ({ target: "leaderboard", requestedAt });

test("only ?arcade=play and ?arcade=leaderboard ask for the arcade", () => {
  assert.equal(requestsArcade("?arcade=play"), true);
  assert.equal(requestsArcade("?conversation=abc&arcade=play"), true);
  assert.equal(requestsArcade("?arcade=leaderboard"), true);
  assert.equal(requestsArcade(""), false);
  assert.equal(requestsArcade("?arcade="), false);
  assert.equal(requestsArcade("?arcade=PLAY"), false);
  assert.equal(requestsArcade("?play=arcade"), false);
  assert.equal(arcadeRequestTarget("?arcade=play"), "game");
  assert.equal(arcadeRequestTarget("?a=1&arcade=leaderboard"), "leaderboard");
  assert.equal(arcadeRequestTarget("?arcade=LEADERBOARD"), null);
  assert.equal(arcadeRequestTarget("?a=1"), null);
});

test("the request leaves the address bar, every other parameter stays", () => {
  assert.equal(withoutArcadeRequest("?arcade=play"), "");
  assert.equal(withoutArcadeRequest("?a=1&arcade=play&b=2"), "?a=1&b=2");
  assert.equal(withoutArcadeRequest("?arcade=leaderboard&a=1"), "?a=1");
  assert.equal(withoutArcadeRequest("?a=1"), "?a=1");
});

test("a reload asks for the game or the leaderboard once, every other parameter kept", () => {
  assert.equal(withArcadeRequest(""), "?arcade=play");
  assert.equal(withArcadeRequest("?c=abc&studio=1"), "?c=abc&studio=1&arcade=play");
  assert.equal(withArcadeRequest("?arcade=play"), "?arcade=play");
  assert.equal(withArcadeRequest("?arcade=nope&a=1"), "?arcade=play&a=1");
  assert.equal(requestsArcade(withArcadeRequest("?a=1")), true);
  assert.equal(withArcadeRequest("?c=abc", "leaderboard"), "?c=abc&arcade=leaderboard");
  assert.equal(withArcadeRequest("?arcade=play", "leaderboard"), "?arcade=leaderboard");
  assert.equal(arcadeRequestTarget(withArcadeRequest("?a=1", "leaderboard")), "leaderboard");
});

test("the reload target is this very page asking for the arcade, prefix, path and hash kept", () => {
  const page = "https://intranet.example.com/xtm-one/presentations/abc?panel=editor#/slide-3";
  assert.equal(
    arcadeRequestUrl(page, "leaderboard"),
    "https://intranet.example.com/xtm-one/presentations/abc?panel=editor&arcade=leaderboard#/slide-3",
  );
  // Already asked for: the very same URL, which the provider reloads rather
  // than assigns (with a hash, assigning it would not even leave the page).
  const asked = "https://copilot.example.com/chat?arcade=play#top";
  assert.equal(arcadeRequestUrl(asked, "game"), asked);
});

test("a request in the link is remembered, so it survives the sign-in", () => {
  const storage = memoryStorage();
  assert.deepEqual(captureArcadeRequest("?arcade=play", storage, 1_000), game(1_000));
  assert.equal(storage.items.get(ARCADE_LINK_STORAGE_KEY), "1000");
  // Back from the identity provider on /login: the query string is gone.
  assert.deepEqual(captureArcadeRequest("", storage, 1_000 + 60_000), game(1_000), "dated from the click");
});

test("a leaderboard request survives the sign-in too, still a leaderboard", () => {
  const storage = memoryStorage();
  assert.deepEqual(captureArcadeRequest("?arcade=leaderboard", storage, 1_000), leaderboard(1_000));
  assert.equal(storage.items.get(ARCADE_LINK_STORAGE_KEY), "leaderboard:1000");
  assert.deepEqual(captureArcadeRequest("", storage, 1_000 + 60_000), leaderboard(1_000), "dated from the click");
  captureArcadeRequest("?arcade=play", storage, 2_000);
  assert.deepEqual(captureArcadeRequest("", storage, 3_000), game(2_000), "the latest request wins");
});

test("a stale request is dropped, never a game long after the click", () => {
  const storage = memoryStorage();
  captureArcadeRequest("?arcade=play", storage, 0);
  assert.equal(captureArcadeRequest("", storage, ARCADE_LINK_TTL_MS), null);
  assert.equal(storage.items.has(ARCADE_LINK_STORAGE_KEY), false);
  assert.equal(captureArcadeRequest("", storage, 0), null, "and stays dropped");
});

test("a request expires during a sign-in that outlasts it", () => {
  assert.equal(isFreshArcadeRequest(0, ARCADE_LINK_TTL_MS - 1), true);
  assert.equal(isFreshArcadeRequest(0, ARCADE_LINK_TTL_MS), false);
  assert.equal(isFreshArcadeRequest(1_000, 0), false, "a clock set back never revives one");
});

test("nothing remembered, nothing requested", () => {
  assert.equal(captureArcadeRequest("", memoryStorage(), 5_000), null);
  assert.equal(captureArcadeRequest("", null, 5_000), null);
});

test("a request honoured once is forgotten", () => {
  const storage = memoryStorage();
  captureArcadeRequest("?arcade=play", storage, 0);
  forgetArcadeRequest(storage);
  assert.equal(captureArcadeRequest("", storage, 1), null);
});

test("an unreadable or future-dated request is forgotten, never honoured", () => {
  const now = Date.UTC(2026, 8, 26);
  for (const value of [
    "not-a-time",
    "Infinity",
    "",
    String(now + 60_000),
    "leaderboard:",
    "leaderboard:soon",
    `leaderboard:${now + 60_000}`,
  ]) {
    const storage = memoryStorage();
    storage.items.set(ARCADE_LINK_STORAGE_KEY, value);
    assert.equal(captureArcadeRequest("", storage, now), null, value);
    assert.equal(storage.items.has(ARCADE_LINK_STORAGE_KEY), false, value);
  }
});

// The gate the provider evaluates (`arcade-gate.ts`), for a signed-in session
// that `/auth/me` and `/auth/config` both answered.
const OPEN: ArcadeGateInputs = {
  signedIn: true,
  impersonating: false,
  termsPending: false,
  arcadeEnabled: true,
  configPending: false,
};
const ready = { requestedAt: 0, now: 1_000, signedIn: true, gate: arcadeGate(OPEN) };
const withGate = (change: Partial<ArcadeGateInputs>) => ({ ...ready, gate: arcadeGate({ ...OPEN, ...change }) });

test("a request plays once the gate is open", () => {
  assert.equal(arcadeLinkVerdict(ready), "play");
});

test("a request waits while the auth state loads, then plays once it opens the gate", () => {
  // A token the server has not accepted yet may be one it refuses: its first
  // request sends the tab back to /login, where the request must still wait.
  assert.equal(arcadeLinkVerdict(withGate({ termsPending: undefined })), "wait");
  assert.equal(arcadeLinkVerdict(withGate({ arcadeEnabled: undefined, configPending: true })), "wait");
  assert.equal(arcadeLinkVerdict(withGate({})), "play");
});

test("a signed-out visit keeps its request for the sign-in", () => {
  // Signed out, the gate reads as closed: that is no answer yet.
  assert.equal(arcadeLinkVerdict({ ...withGate({ signedIn: false }), signedIn: false }), "wait");
});

test("a request is held on the Terms of Use waiting screen, never opened there", () => {
  assert.equal(arcadeLinkVerdict(withGate({ termsPending: true })), "wait");
});

test("a request held across the Terms of Use plays once they are accepted", () => {
  const held = withGate({ termsPending: true });
  assert.equal(arcadeLinkVerdict(held), "wait");
  assert.equal(arcadeLinkVerdict({ ...held, now: 5 * 60_000, gate: arcadeGate(OPEN) }), "play");
});

test("a request held across the Terms of Use expires after 10 minutes", () => {
  const held = withGate({ termsPending: true });
  assert.equal(arcadeLinkVerdict({ ...held, now: ARCADE_LINK_TTL_MS - 1 }), "wait");
  assert.equal(arcadeLinkVerdict({ ...held, now: ARCADE_LINK_TTL_MS }), "drop");
  assert.equal(arcadeLinkVerdict({ ...held, now: ARCADE_LINK_TTL_MS, gate: arcadeGate(OPEN) }), "drop", "accepted too late");
});

test("a request never opens while impersonating", () => {
  assert.equal(arcadeLinkVerdict(withGate({ impersonating: true })), "drop");
  assert.equal(arcadeLinkVerdict(withGate({ impersonating: true, termsPending: true })), "drop", "Terms pending or not");
});

test("a request is dropped with the arcade off, or once expired, signed in or not", () => {
  assert.equal(arcadeLinkVerdict(withGate({ arcadeEnabled: false })), "drop");
  assert.equal(arcadeLinkVerdict(withGate({ arcadeEnabled: false, termsPending: true })), "drop", "Terms pending or not");
  assert.equal(arcadeLinkVerdict({ ...ready, now: ARCADE_LINK_TTL_MS }), "drop");
  assert.equal(arcadeLinkVerdict({ ...ready, now: ARCADE_LINK_TTL_MS, signedIn: false }), "drop");
  assert.equal(arcadeLinkVerdict({ ...ready, requestedAt: 5_000 }), "drop", "a clock set back");
});

test("storage that throws only costs the survival across sign-in", () => {
  assert.deepEqual(captureArcadeRequest("?arcade=play", brokenStorage, 0), game(0));
  assert.deepEqual(captureArcadeRequest("?arcade=leaderboard", brokenStorage, 0), leaderboard(0));
  assert.equal(captureArcadeRequest("", brokenStorage, 0), null);
  assert.doesNotThrow(() => forgetArcadeRequest(brokenStorage));
});
