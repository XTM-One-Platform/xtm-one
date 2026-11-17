/**
 * Unit tests for which files of an agent reply are cards and how two files of
 * the same name tell apart - `yarn test:unit`.
 *
 * A turn that called `generate_file` twice under one name showed two cards
 * reading `TXT - 18.1 KB`: the reply named the second file by marker, and the
 * first was attached anyway with nothing to tell it was the stale one.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  conversationRevisions,
  fileCardFacts,
  fileTag,
  newerRevisionId,
} from "./file-revisions.ts";

const NAME = "MinArm-leak-analysis-run-prompt-2026-10-01.txt";

function file(id: string, filename: string, extra: Record<string, unknown> = {}) {
  return {
    filename,
    type: "txt",
    size: 18_534,
    file_id: id,
    content_type: "text/plain; charset=utf-8",
    ...extra,
  };
}

test("(c) revisions are numbered across the conversation, the time falls back to the message's", () => {
  const revisions = conversationRevisions([
    { created_at: "2026-10-01T09:00:00Z", attachments: [file("1111", NAME)] },
    { created_at: "2026-10-01T10:00:00Z", attachments: [file("2222", "other.txt")] },
    {
      created_at: "2026-10-01T14:33:00Z",
      attachments: [
        file("3333", NAME, { created_at: "2026-10-01T14:32:40Z" }),
        file("4444", NAME, { created_at: "2026-10-01T14:31:02Z" }),
      ],
    },
  ]);
  assert.deepEqual(revisions.get("1111"), { revision: 1, count: 3, createdAt: "2026-10-01T09:00:00Z", latestId: "3333" });
  assert.deepEqual(revisions.get("4444"), { revision: 2, count: 3, createdAt: "2026-10-01T14:31:02Z", latestId: "3333" });
  assert.deepEqual(revisions.get("3333"), { revision: 3, count: 3, createdAt: "2026-10-01T14:32:40Z", latestId: "3333" });
  assert.deepEqual(revisions.get("2222"), { revision: 1, count: 1, createdAt: "2026-10-01T10:00:00Z", latestId: "2222" });
});

test("(c) only a superseded revision points to the newer version, the newest and a lone file do not", () => {
  const revisions = conversationRevisions([
    { created_at: "2026-10-01T09:00:00Z", attachments: [file("AAAA", NAME)] },
    { created_at: "2026-10-01T10:00:00Z", attachments: [file("bbbb", NAME), file("cccc", "other.txt")] },
  ]);
  assert.equal(newerRevisionId(revisions.get("aaaa")), "bbbb");
  assert.equal(newerRevisionId(revisions.get("bbbb")), null);
  assert.equal(newerRevisionId(revisions.get("cccc")), null);
  assert.equal(newerRevisionId(null), null);
});

test("(c) a file shown twice (an earlier file named again by marker) counts once", () => {
  const att = file("5555", NAME);
  const revisions = conversationRevisions([
    { created_at: "2026-10-01T09:00:00Z", attachments: [att] },
    { created_at: "2026-10-01T09:05:00Z", attachments: [att] },
  ]);
  assert.deepEqual(revisions.get("5555"), { revision: 1, count: 1, createdAt: "2026-10-01T09:00:00Z", latestId: "5555" });
});

test("(c) the card shows its time always, the badge and exact bytes only on a collision", () => {
  const sha = "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08";
  const att = file("6666", NAME, { created_at: "2026-10-01T14:32:40Z", sha256: sha });
  assert.deepEqual(fileCardFacts(att, { revision: 1, count: 1, createdAt: "2026-10-01T14:32:40Z", latestId: "6666" }), {
    createdAt: "2026-10-01T14:32:40Z",
    revision: null,
    exactBytes: null,
    fileId: "6666",
    shaPrefix: "9f86d081884c",
  });
  assert.deepEqual(fileCardFacts(att, { revision: 2, count: 2, createdAt: "2026-10-01T14:32:40Z", latestId: "6666" }), {
    createdAt: "2026-10-01T14:32:40Z",
    revision: 2,
    exactBytes: 18_534,
    fileId: "6666",
    shaPrefix: "9f86d081884c",
  });
  // Outside a transcript (no revision known): the file's own time, no badge.
  const facts = fileCardFacts(file("7777", NAME), null);
  assert.equal(facts.createdAt, null);
  assert.equal(facts.revision, null);
  assert.equal(facts.shaPrefix, null);
});

test("(d) file_tag decides card or chip", () => {
  assert.equal(fileTag({ file_tag: "working_file" }), "working_file");
  assert.equal(fileTag({ file_tag: "download_file" }), "download_file");
  assert.equal(fileTag({ file_tag: null }), "download_file");
  assert.equal(fileTag({}), "download_file");
});
