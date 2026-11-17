/**
 * Unit tests for the integration-event filter editor's helpers - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { cleanEventFilterExtra, pickEventFilterExtra, splitList } from "./event-filters.ts";

test("the editor leaves the Scope pickers and the sender text to the trigger card", () => {
  const extra = pickEventFilterExtra({
    scope: { teams_channel: ["t:c"] },
    channels: ["t:c"],
    from_contains: "events@",
    exclude_automated: true,
  });
  assert.deepEqual(extra, { exclude_automated: true });
});

test("a mail filter keeps only what is set, the subjects as a list", () => {
  const cleaned = cleanEventFilterExtra(
    {
      from: { allow: [], deny: ["crowdstrike.com"] },
      to_me: false,
      cc_me: true,
      exclude_automated: true,
      subject_contains: "board, audit",
      folder: "  ",
      chat_ids: ["19:x"],
    },
    "new_email",
  );
  assert.deepEqual(cleaned, {
    from: { deny: ["crowdstrike.com"] },
    cc_me: true,
    exclude_automated: true,
    subject_contains: ["board", "audit"],
  });
});

test("each event keeps the keys it takes", () => {
  const extra = {
    exclude_authors: ["Build Bot"],
    chat_ids: ["19:a@thread.v2"],
    change_kinds: ["cancelled" as const],
    flag_status: ["flagged" as const],
    exclude_automated: true,
  };
  assert.deepEqual(cleanEventFilterExtra(extra, "new_teams_reply"), { exclude_authors: ["Build Bot"] });
  assert.deepEqual(cleanEventFilterExtra(extra, "new_teams_chat_message"), {
    exclude_authors: ["Build Bot"],
    chat_ids: ["19:a@thread.v2"],
  });
  assert.deepEqual(cleanEventFilterExtra(extra, "calendar_event_changed"), { change_kinds: ["cancelled"] });
  assert.deepEqual(cleanEventFilterExtra(extra, "email_flag_changed"), {
    exclude_automated: true,
    flag_status: ["flagged"],
  });
});

test("a To Do trigger keeps its lists, change kinds and exclude_self", () => {
  const extra = {
    lists: ["Tasks"],
    exclude_lists: ["Flagged Emails"],
    change_kinds: ["created" as const],
    exclude_self: true,
    exclude_authors: ["Build Bot"],
  };
  assert.deepEqual(cleanEventFilterExtra(extra, "todo_task_changed"), {
    lists: ["Tasks"],
    exclude_lists: ["Flagged Emails"],
    change_kinds: ["created"],
    exclude_self: true,
  });
  assert.deepEqual(cleanEventFilterExtra(extra, "calendar_event_changed"), { change_kinds: ["created"] });
});

test("a comma-separated list drops blanks", () => {
  assert.deepEqual(splitList(" a , ,b,"), ["a", "b"]);
});
