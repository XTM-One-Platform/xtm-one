/**
 * What a game waits for - `yarn test:unit`.
 *
 * A surface reports what `XtmChatPanel` hands `useArcadeWaitSignal` after
 * each frame of a turn: the conversation on screen and its working state
 * (`useTurnFrames().working`, the panel's `onTurnRunningChange`), held through
 * an intermediate `done`, dropped by the final `done`, an `error` or a
 * `cancelled` frame.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { WaitSources } from "./wait-sources.ts";

const working = (conversationId: string | null) => ({ conversationId, working: true });
const idle = (conversationId: string | null) => ({ conversationId, working: false });

test("an intermediate segment is not the answer: the wait holds through the tool calls, the final done ends it", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a")); // the send, then the `thinking` ack
  waits.watch("chat");
  waits.report("chat", working("a")); // `stream`: a provisional segment
  waits.report("chat", working("a")); // `done` flagged intermediate, then its `thinking` re-arm
  waits.report("chat", working("a")); // more tool calls, then the final answer streams
  assert.equal(waits.answer, "waiting");
  waits.report("chat", idle("a")); // the final `done`
  assert.equal(waits.answer, "ready");
});

test("an error or a cancel of the awaited turn ends the wait", () => {
  for (const frame of ["error", "cancelled"]) {
    const waits = new WaitSources();
    waits.report("chat", working("a"));
    waits.watch("chat");
    waits.report("chat", idle("a")); // the `error` / `cancelled` frame
    assert.equal(waits.answer, "ready", frame);
  }
});

test("what happens on another conversation never answers", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.report("studio", working("b"));
  waits.watch("chat");
  waits.report("studio", idle("b")); // another surface's conversation ends its turn
  assert.equal(waits.answer, "waiting");

  waits.report("chat", idle("c")); // the chat now shows another conversation, idle
  assert.equal(waits.answer, "none");
});

test("Back / Forward to another conversation is not the answer, and coming back does not make it one", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.watch("chat");
  // Back: the stale render still carries the working state of `a`, then the
  // stream of `a` closes and the one of `b` opens.
  waits.report("chat", working("b"));
  waits.report("chat", idle("b"));
  assert.equal(waits.answer, "none");
  // Forward: `a` is resumed into its turn, which then ends.
  waits.report("chat", working("a"));
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "none");
});

test("a new conversation opened while waiting is not the answer", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.watch("chat");
  waits.report("chat", idle(null));
  assert.equal(waits.answer, "none");
});

test("the first turn of a new conversation is waited for like any other", () => {
  // Both surfaces create the conversation before sending. The main chat's
  // `?c=<id>` lands in a navigation transition, after the render the send's
  // working state triggered: one frame shows the turn on no conversation yet.
  const waits = new WaitSources();
  waits.report("chat", idle(null)); // the new-conversation screen
  waits.report("chat", working(null)); // the send
  waits.report("chat", working("a")); // the conversation it created
  waits.watch("chat"); // Take control, a few seconds into the turn
  waits.report("chat", working("a")); // an intermediate segment, then more tool calls
  assert.equal(waits.answer, "waiting");
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "ready");
});

test("a new conversation getting its id under a running game is not a conversation left", () => {
  const waits = new WaitSources();
  waits.report("chat", idle(null));
  waits.report("chat", working(null)); // the send
  waits.watch(); // the code, typed in that very frame
  waits.report("chat", working("a")); // the conversation that turn created
  assert.equal(waits.answer, "waiting");
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "ready");
});

test("a surface that goes away is not an answer: the game plays on", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.watch("chat");
  waits.drop("chat");
  assert.equal(waits.answer, "none");
  assert.equal(waits.any, false);

  waits.report("chat", working("a"));
  waits.report("studio", working("b"));
  waits.watch();
  waits.drop("studio");
  assert.equal(waits.answer, "waiting");
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "ready");
});

test("a turn that starts after the game started does not count, on any surface", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.watch();
  waits.report("studio", working("b"));
  waits.report("studio", idle("b"));
  assert.equal(waits.answer, "waiting");
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "ready");
});

test("a game started while nothing waited never hears of an answer, not even from the next turn", () => {
  const waits = new WaitSources();
  waits.report("chat", idle("a"));
  waits.watch("chat");
  assert.equal(waits.answer, "none");
  waits.report("chat", working("a"));
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "none");
});

test("a game launched from a surface hears of that surface's answer, whatever the others do", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.report("studio", working("b"));
  waits.watch("chat");
  waits.report("chat", idle("a"));
  assert.equal(waits.answer, "ready");
  assert.equal(waits.any, true);
});

test("a game launched from nowhere in particular hears of the first answer", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.report("studio", working("b"));
  waits.watch();
  waits.report("studio", idle("b"));
  assert.equal(waits.answer, "ready");
});

test("a launching surface that is not waiting falls back to the ones that are", () => {
  const waits = new WaitSources();
  waits.report("chat", idle("a"));
  waits.report("studio", working("b"));
  waits.watch("chat");
  assert.equal(waits.answer, "waiting");
  waits.unwatch();
  assert.equal(waits.answer, "none");
});

test("an answer that arrived stays ready until the game ends", () => {
  const waits = new WaitSources();
  waits.report("chat", working("a"));
  waits.watch("chat");
  waits.report("chat", idle("a"));
  waits.report("chat", working("b"));
  waits.drop("chat");
  assert.equal(waits.answer, "ready");
  waits.unwatch();
  assert.equal(waits.answer, "none");
});

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

test("the chat publishes the turn in flight on the conversation on screen, never the streamed text", () => {
  const call = read("../chat-panel/XtmChatPanel.tsx").match(/useArcadeWaitSignal\(([^)]*)\)/);
  assert.ok(call, "XtmChatPanel publishes the wait through useArcadeWaitSignal");
  // A provisional segment streams exactly like a final answer: only
  // the turn's end tells them apart.
  assert.doesNotMatch(call[1], /streaming/i);
  assert.match(call[1], /^\s*conversationId\s*,\s*working\s*$/);
});

test("the studio rail feeds that wait the turn's own state, never an attachment upload", () => {
  // The panel says when a turn runs; an upload is not a turn, and one running
  // across the turn's end would hold that end back.
  const props = read("../studio/StudioRail.tsx").match(/<LazyXtmChatPanel\b([\s\S]*?)\/>/);
  assert.ok(props, "the studio rail renders the chat panel");
  assert.match(props[1], /\bworking=\{frames\.working\}/);
});
