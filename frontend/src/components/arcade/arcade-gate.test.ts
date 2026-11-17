/**
 * Whether the waiting arcade may open - `yarn test:unit`.
 *
 * One predicate (`arcadeGate`), evaluated once by `ArcadeProvider`; every
 * way in reads its answer - the hidden code, the palette command, the
 * waiting panel's Take control, the leaderboard and its Play, the links - and
 * none derives one of its own.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import { arcadeGate, type ArcadeGateInputs } from "./arcade-gate.ts";

const OPEN: ArcadeGateInputs = {
  signedIn: true,
  impersonating: false,
  termsPending: false,
  arcadeEnabled: true,
  configPending: false,
};

const CLOSING: Record<string, Partial<ArcadeGateInputs>> = {
  "signed out": { signedIn: false },
  impersonating: { impersonating: true },
  "arcade disabled": { arcadeEnabled: false },
};

test("Terms of Use to accept: closed, and said so apart from the other reasons", () => {
  // `consent` is closed like the rest (nothing but `open` opens the arcade);
  // it is the one closed reason a `?arcade=play` link waits out.
  assert.equal(arcadeGate({ ...OPEN, termsPending: true }), "consent");
  assert.equal(arcadeGate({ ...OPEN, termsPending: true, arcadeEnabled: undefined, configPending: true }), "consent");
  for (const [condition, change] of Object.entries(CLOSING)) {
    assert.equal(arcadeGate({ ...OPEN, termsPending: true, ...change }), "closed", `${condition} wins over the Terms of Use`);
  }
});

test("signed in, Terms of Use accepted, not impersonating and the arcade on: it opens", () => {
  assert.equal(arcadeGate(OPEN), "open");
});

for (const [condition, change] of Object.entries(CLOSING)) {
  test(`${condition}: the arcade stays closed, even before the rest is known`, () => {
    assert.equal(arcadeGate({ ...OPEN, ...change }), "closed");
    const loading = { ...OPEN, termsPending: undefined, arcadeEnabled: undefined, configPending: true };
    assert.equal(arcadeGate({ ...loading, ...change }), "closed");
  });
}

test("undecided while /auth/me or /auth/config are on their way", () => {
  assert.equal(arcadeGate({ ...OPEN, termsPending: undefined }), "pending");
  assert.equal(arcadeGate({ ...OPEN, arcadeEnabled: undefined, configPending: true }), "pending");
});

test("a config that failed or predates the flag keeps the default: on", () => {
  assert.equal(arcadeGate({ ...OPEN, arcadeEnabled: undefined }), "open");
});

// Every way in reads that one answer.

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const PROVIDER = read("./ArcadeProvider.tsx");

test("the provider derives the gate once, from the predicate and the inputs it names", () => {
  assert.equal(PROVIDER.match(/\barcadeGate\(/g)?.length, 1);
  assert.match(PROVIDER, /const enabled = gate === "open";/);
  // The Terms of Use are read the way `TermsGate` reads them.
  assert.match(PROVIDER, /termsOfUsePending\(/);
  assert.match(read("../../App.tsx"), /if \(termsOfUsePending\(terms\)\) return <TermsOfUse/);
});

test("the provider reads the Terms of Use once, the way TermsGate does", () => {
  // `/auth/me` itself through `useMeAboveRoutes` (hooks/api/me-query.test.ts):
  // whatever else of `me` the provider needs, its Terms of Use are never
  // re-tested inline next to the gate.
  assert.match(PROVIDER, /\buseMeAboveRoutes\(/);
  assert.deepEqual(PROVIDER.match(/\.terms_of_use\b/g), [".terms_of_use"]);
  assert.match(PROVIDER, /termsOfUsePending\(me\.terms_of_use\)/);
});

test("nothing else in the arcade re-derives it", () => {
  const files = readdirSync(new URL(".", import.meta.url))
    .filter((name) => /\.tsx?$/.test(name) && !name.endsWith(".test.ts") && name !== "ArcadeProvider.tsx")
    .map((name) => `./${name}`)
    .concat("../chat/ChatWaitingGame.tsx", "../chat/waiting-arcade.ts");
  for (const file of files) {
    assert.doesNotMatch(
      read(file),
      /\b(getToken|isImpersonating|termsOfUsePending|useMe)\s*\(|arcade_enabled|terms_of_use/,
      `${file} reads an input of the gate: ask \`useArcade().enabled\` instead`,
    );
  }
});

test("the hidden code listens only while the gate is open", () => {
  assert.match(PROVIDER, /if \(!enabled \|\| session\) return;\s*const listener = new SecretCodeListener/);
});

test("the palette offers the arcade only while the gate is open", () => {
  const manifest = read("./arcade.commands.ts");
  const commands = manifest.match(/id: "arcade\./g)?.length ?? 0;
  assert.ok(commands > 0);
  assert.equal(manifest.match(/when: \["arcadeEnabled"\]/g)?.length, commands);
  assert.match(read("../../lib/commands/CommandsProvider.tsx"), /const arcadeEnabled = useArcade\(\)\.enabled;/);
});

test("the waiting panel offers the game only while the gate is open", () => {
  // Its one launch path (`useWaitingArcade`, #3877); that it goes through it
  // is pinned by components/chat/waiting-arcade.test.ts.
  const offer = read("../chat/waiting-arcade.ts");
  assert.match(offer, /const canTakeControl = playMode && arcade\.enabled;/);
  assert.match(offer, /if \(!canTakeControl\) return;\s*arcade\.launch\(/);
});

test("a ?arcade=play link is decided by the gate, not by a reading of its own", () => {
  assert.match(PROVIDER, /arcadeLinkVerdict\(\{[^}]*\bgate\b[^}]*\}\)/);
  assert.doesNotMatch(read("./arcade-link.ts"), /configSettled|arcadeEnabled/);
});

test("every launch, the leaderboard's Play and a link included, is refused unless the gate is open", () => {
  assert.match(PROVIDER, /const launch = useCallback\(\s*\(options: ArcadeLaunchOptions\) => \{\s*if \(!enabled \|\| sessionRef\.current\) return;/);
  // The leaderboard opens through `openLeaderboard()` alone, a
  // `?arcade=leaderboard` link included.
  assert.match(PROVIDER, /if \(enabled\) setLeaderboardOpen\(true\);/);
  assert.equal(PROVIDER.match(/setLeaderboardOpen\(true\)/g)?.length, 1);
  assert.match(PROVIDER, /if \(linkRequest\.target === "leaderboard"\) openLeaderboard\(\);/);
  // A game starts through `launch`, or as a replay from inside a running one.
  assert.equal(PROVIDER.match(/\bstartGame\(/g)?.length, 2);
  assert.match(PROVIDER, /startGame\(options\);/);
  assert.match(PROVIDER, /startGame\(\{ source: "replay"/);
});

test("every call of launch() is a way in named here", () => {
  // A new one - a button, a command, a link - joins this list with the proof
  // that it is offered only while the gate is open and, from a chat surface,
  // that it waits on that surface (`waitSource`), like the waiting panel.
  const src = new URL("../../", import.meta.url);
  const callers = readdirSync(src, { recursive: true })
    .map((path) => String(path).replace(/\\/g, "/"))
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => /\blaunch\(\{/.test(readFileSync(new URL(path, src), "utf8")))
    .sort();
  assert.deepEqual(callers, [
    "components/arcade/ArcadeProvider.tsx", // the code, the leaderboard's Play, a link
    "components/chat/waiting-arcade.ts", // Take control, above
    "components/layout/AppLayout.tsx", // the palette command, above
    "components/layout/Header.tsx", // the user menu's Play Space Invaders
  ]);
  assert.match(read("../layout/AppLayout.tsx"), /useCommandHandler\("arcade\.play", \(\) => arcade\.launch\(/);
  // The user menu offers its two entries only while the gate is open, and its
  // leaderboard entry goes through `openLeaderboard()`.
  assert.match(
    read("../layout/Header.tsx"),
    /\{arcade\.enabled && \([\s\S]*?arcade\.launch\(\{ source: "menu", origin: null \}\)[\s\S]*?arcade\.openLeaderboard\(\)/,
  );
  // From a chat surface, the game waits on that surface.
  assert.match(read("../chat/waiting-arcade.ts"), /arcade\.launch\(\{ source: "waiting", origin, waitSource \}\)/);
});
