/**
 * Unit tests for where the chat page lands and how its list follows a send -
 * `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  bumpConversation,
  lastConversationStorageKey,
  navigationArmsResume,
  pickResumeConversation,
  resolveResume,
  resumesOnArrival,
  resumesOnNavigation,
} from "./chat-navigation.ts";

const row = (id: string, extra: Partial<{ is_archived: boolean; last_message_preview: string | null }> = {}) => ({
  id,
  updated_at: "2026-11-01T10:00:00.000Z",
  is_archived: false,
  last_message_preview: `preview ${id}`,
  ...extra,
});

test("Chat resumes the conversation last opened while the list still holds it", () => {
  const list = [row("a"), row("b"), row("c")];
  assert.equal(pickResumeConversation(list, "c"), "c");
});

test("Chat resumes the most recent conversation when the last opened one is gone or archived", () => {
  assert.equal(pickResumeConversation([row("a"), row("b")], "deleted"), "a");
  assert.equal(pickResumeConversation([row("a"), row("b", { is_archived: true })], "b"), "a");
  assert.equal(pickResumeConversation([row("a", { is_archived: true }), row("b")], null), "b");
});

test("Chat opens a new conversation when there is none to resume", () => {
  assert.equal(pickResumeConversation([], "a"), null);
  assert.equal(pickResumeConversation(undefined, null), null);
});

test("Chat after archiving the open conversation resumes the most recent remaining one", () => {
  // The stored id is cleared with the archive; the list no longer holds it.
  assert.equal(pickResumeConversation([row("b"), row("c")], null), "b");
  assert.equal(pickResumeConversation([row("a", { is_archived: true }), row("b")], "a"), "b");
});

const pending = { failed: false, lastOpenedId: null, activeConvId: null, typing: false };

test("the placeholder holds while the list loads", () => {
  assert.deepEqual(resolveResume({ ...pending, conversations: undefined }), {
    settled: false,
    open: null,
    keepPlaceholder: true,
  });
});

test("a picked conversation keeps the placeholder until it is open: no welcome frame in between", () => {
  assert.deepEqual(resolveResume({ ...pending, conversations: [row("a"), row("b")], lastOpenedId: "b" }), {
    settled: true,
    open: "b",
    keepPlaceholder: true,
  });
});

test("the welcome screen shows at once when there is nothing to resume or the list failed", () => {
  assert.deepEqual(resolveResume({ ...pending, conversations: [] }), { settled: true, open: null, keepPlaceholder: false });
  assert.deepEqual(resolveResume({ ...pending, conversations: undefined, failed: true }), {
    settled: true,
    open: null,
    keepPlaceholder: false,
  });
});

test("the resume steps aside when the person already opened a conversation or started typing", () => {
  const list = [row("a")];
  assert.deepEqual(resolveResume({ ...pending, conversations: list, activeConvId: "x" }), {
    settled: true,
    open: null,
    keepPlaceholder: false,
  });
  assert.deepEqual(resolveResume({ ...pending, conversations: list, typing: true }), {
    settled: true,
    open: null,
    keepPlaceholder: false,
  });
});

test("a pushed navigation to the bare /chat resumes", () => {
  assert.equal(navigationArmsResume({ type: "PUSH", previousSearch: "?c=a", search: "" }), true);
});

test("Chat clicked on the bare /chat itself resumes: the router replaces a link to the same address", () => {
  assert.equal(navigationArmsResume({ type: "REPLACE", previousSearch: "", search: "" }), true);
});

test("the page's own address rewrites and back / forward do not resume", () => {
  assert.equal(navigationArmsResume({ type: "REPLACE", previousSearch: "?new=1", search: "" }), false);
  assert.equal(navigationArmsResume({ type: "REPLACE", previousSearch: "?investigate=x&agent=y", search: "" }), false);
  assert.equal(navigationArmsResume({ type: "POP", previousSearch: "?c=a", search: "" }), false);
});

test("the last conversation is remembered per person", () => {
  assert.notEqual(lastConversationStorageKey("u1"), lastConversationStorageKey("u2"));
});

test("a send moves the conversation first, touched now, with its new preview", () => {
  const list = [row("a"), row("b"), row("c")];
  const next = bumpConversation(list, "c", { at: "2026-11-02T07:00:00.000Z", preview: "  hello  " });
  assert.deepEqual(next?.map((c) => c.id), ["c", "a", "b"]);
  assert.equal(next?.[0].updated_at, "2026-11-02T07:00:00.000Z");
  assert.equal(next?.[0].last_message_preview, "hello");
  assert.equal(list[2].updated_at, "2026-11-01T10:00:00.000Z", "the cached list is not mutated");
});

test("a send without text keeps the preview, and a long one is cut", () => {
  const kept = bumpConversation([row("a"), row("b")], "b", { at: "2026-11-02T07:00:00.000Z", preview: "   " });
  assert.equal(kept?.[0].last_message_preview, "preview b");
  const cut = bumpConversation([row("a")], "a", { at: "2026-11-02T07:00:00.000Z", preview: "x".repeat(500) });
  assert.equal(cut?.[0].last_message_preview?.length, 200);
});

test("a list that does not hold the conversation, or no list, is returned as it is", () => {
  const list = [row("a")];
  assert.equal(bumpConversation(list, "z", { at: "2026-11-02T07:00:00.000Z" }), list);
  assert.equal(bumpConversation(undefined, "a", { at: "2026-11-02T07:00:00.000Z" }), undefined);
});

const address = (query: string) => new URLSearchParams(query);
const noLaunch = { pendingAgent: null, draft: "", autoSend: false };

test("arriving on the bare /chat resumes a conversation", () => {
  assert.equal(resumesOnArrival(address(""), noLaunch), true);
  // A parameter of its own (a deep link) is no reason to start a new chat.
  assert.equal(resumesOnArrival(address("arcade=play"), noLaunch), true);
});

test("arriving with a conversation, ?new=1, a launch or a launcher's hand-off does not resume", () => {
  for (const query of ["c=abc", "new=1", "agent=a1", "investigate=apt29"]) {
    assert.equal(resumesOnArrival(address(query), noLaunch), false, query);
  }
  assert.equal(resumesOnArrival(address(""), { ...noLaunch, pendingAgent: "a1" }), false, "an agent handed off");
  assert.equal(resumesOnArrival(address(""), { ...noLaunch, draft: "summarize the alert" }), false, "a primed draft");
  assert.equal(resumesOnArrival(address(""), { ...noLaunch, draft: "   " }), true, "a blank draft is none");
  assert.equal(resumesOnArrival(address(""), { ...noLaunch, autoSend: true }), false, "an auto-send");
});

test("the Chat entry clicked on the page resumes, the page's own leaving and history moves do not", () => {
  const pushed = { type: "PUSH", previousSearch: "?c=a", search: "", leavingConversation: false } as const;
  assert.equal(resumesOnNavigation(address(""), pushed), true);
  assert.equal(resumesOnNavigation(address(""), { ...pushed, leavingConversation: true }), false, "New conversation");
  assert.equal(resumesOnNavigation(address(""), { ...pushed, type: "POP" }), false, "back or forward");
  assert.equal(resumesOnNavigation(address(""), { ...pushed, type: "REPLACE" }), false, "the page's own rewrite");
  // "Chat" clicked on the bare /chat: the router replaces the address with itself.
  assert.equal(resumesOnNavigation(address(""), { ...pushed, type: "REPLACE", previousSearch: "" }), true, "Chat on /chat");
  assert.equal(resumesOnNavigation(address("c=abc"), pushed), false, "a conversation picked");
  assert.equal(resumesOnNavigation(address("new=1"), pushed), false, "a link to a new conversation");
});

