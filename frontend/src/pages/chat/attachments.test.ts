/**
 * Unit tests for what an attached file looks like before the conversation is
 * refetched, and for what a paste or a drop attaches - `yarn test:unit`.
 *
 * The Presentation Studio labelled a pasted screenshot `image/png`, which the
 * transcript does not preview: it read as a file card with no download until
 * the end of the turn swapped in the persisted row. The label is now the
 * backend's, checked against the table both sides share. What a paste attaches
 * is checked against the clipboard of each source, from the table the e2e
 * spec pastes too.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  conversationFileMap,
  fileTypeLabel,
} from "./attachments.ts";

interface TypeCase {
  name: string;
  filename: string;
  content_type: string | null;
  expected: string;
}

const readFixture = <T>(name: string): T =>
  JSON.parse(readFileSync(new URL(`../../../tests_e2e/fixtures/${name}`, import.meta.url), "utf8")) as T;

const { cases } = readFixture<{ cases: TypeCase[] }>("attachment-type-cases.json");
test("the type label is the backend's, case for case", () => {
  assert.ok(cases.length > 0);
  for (const c of cases) {
    assert.equal(fileTypeLabel(c.filename, c.content_type), c.expected, c.name);
  }
});

test("a reply's marker finds the chip of a file an earlier message shows", () => {
  const upload = {
    filename: "report.pdf",
    type: "pdf",
    size: 10,
    file_id: "4B1E8C2A-0F5D-4A7E-9C3B-2D1F0E9A8B7C",
    download_url: "/api/v1/chat/conversations/c/messages/m1/files/4b1e8c2a-0f5d-4a7e-9c3b-2d1f0e9a8b7c/download",
  };
  const later = { ...upload, download_url: "/api/v1/chat/conversations/c/messages/m9/files/x/download" };
  const pending = { filename: "draft.png", type: "png", size: 1, preview: "blob:local" };
  const files = conversationFileMap([
    { attachments: [upload] },
    { attachments: null },
    { attachments: [later, pending] },
  ]);
  // Keyed like the markers (lower case), the first message showing the file wins.
  assert.equal(files.get("4b1e8c2a-0f5d-4a7e-9c3b-2d1f0e9a8b7c"), upload);
  // An attachment with nothing to download (an optimistic echo) is no chip.
  assert.equal(files.size, 1);
});

