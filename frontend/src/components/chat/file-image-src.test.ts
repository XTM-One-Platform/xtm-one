/**
 * `![alt](file:<uuid>)` in a chat answer - `yarn test:unit`.
 *
 * The reference becomes the platform's download path, which the chat shows
 * inline through an authenticated fetch; everything else stays as written.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  chatFileDownloadPath,
  chatFileIdFromHref,
  fileSchemeImagesToApi,
  fileSchemeLinksToApi,
  normalizeApiImageMarkdown,
  prepareChatImages,
} from "./file-image-src.ts";

const ID = "8bf09217-bdf2-4d3a-ae6d-7912a28879da";
const PATH = `/api/v1/chat/files/${ID}/download`;

test("a file: image becomes the authenticated download path, alt text kept", () => {
  assert.equal(
    fileSchemeImagesToApi(`Scan this:\n\n![WhatsApp pairing QR](file:${ID})\n\nThen confirm.`),
    `Scan this:\n\n![WhatsApp pairing QR](${PATH})\n\nThen confirm.`,
  );
});

test("an upper-case id, angle brackets, a title and an empty alt are all read", () => {
  assert.equal(fileSchemeImagesToApi(`![qr](file:${ID.toUpperCase()})`), `![qr](${PATH})`);
  assert.equal(fileSchemeImagesToApi(`![qr](<file:${ID}>)`), `![qr](${PATH})`);
  assert.equal(fileSchemeImagesToApi(`![qr](file:${ID} "the code")`), `![qr](${PATH})`);
  assert.equal(fileSchemeImagesToApi(`![](file:${ID})`), `![](${PATH})`);
});

test("several images in one answer are each rewritten", () => {
  const other = "e5e8b5f4-a3c9-4024-8c12-097ebeef556b";
  assert.equal(
    fileSchemeImagesToApi(`![a](file:${ID}) and ![b](file:${other})`),
    `![a](${PATH}) and ![b](${chatFileDownloadPath(other)})`,
  );
});

test("anything that is not a file: image of a uuid is left as written", () => {
  const unchanged = [
    `[[FILE:${ID}]]`,
    `[the file](file:${ID})`,
    "![local](file:///Users/me/chart.png)",
    "![x](file:not-a-uuid)",
    `![x](file:${ID}0)`,
    `![x](${PATH})`,
    "![x](https://cdn.example/x.png)",
    "plain text with file: in it",
    "",
  ];
  for (const text of unchanged) assert.equal(fileSchemeImagesToApi(text), text, text);
});

test("a file: image, prose, then an API image: both images and the prose are kept", () => {
  const other = "e5e8b5f4-a3c9-4024-8c12-097ebeef556b";
  const api = `/api/v1/chat/files/${other}/download`;
  const answer = `Start.\n\n![QR first](file:${ID})\n\nMIDDLE-TEXT-MUST-SHOW\n\n![Generated](${api})\n\nEnd.`;
  assert.equal(
    prepareChatImages(answer),
    `Start.\n\n![QR first](${PATH})\n\nMIDDLE-TEXT-MUST-SHOW\n\n![Generated](${api})\n\nEnd.`,
  );
});

test("the API image normaliser flattens a multi-line alt, keeps its words and strips an absolute origin", () => {
  assert.equal(
    normalizeApiImageMarkdown(`![a long\nprompt\nalt](https://one.example/api/v1/chat/images/abc.png)`),
    "![a long prompt alt](/api/v1/chat/images/abc.png)",
  );
  assert.equal(
    normalizeApiImageMarkdown("![x](https://cdn.example/x.png) then ![y](/api/v1/chat/files/f/download)"),
    "![x](https://cdn.example/x.png) then ![y](/api/v1/chat/files/f/download)",
  );
});

test("an API image keeps the model's alt text and an empty alt stays empty", () => {
  assert.equal(
    normalizeApiImageMarkdown("![Q3 revenue by region](/api/v1/chat/images/q3.png)"),
    "![Q3 revenue by region](/api/v1/chat/images/q3.png)",
  );
  assert.equal(normalizeApiImageMarkdown("![](/api/v1/chat/images/q3.png)"), "![](/api/v1/chat/images/q3.png)");
  assert.equal(
    normalizeApiImageMarkdown("![  a [draft]\n chart  ](/api/v1/chat/images/q3.png)"),
    "![a draft chart](/api/v1/chat/images/q3.png)",
  );
});

test("a file: link becomes the download path, its text kept", () => {
  assert.equal(
    fileSchemeLinksToApi(`See [the report](file:${ID}) and ![qr](file:${ID}).`),
    `See [the report](${PATH}) and ![qr](file:${ID}).`,
  );
  assert.equal(fileSchemeLinksToApi(`[r](<file:${ID.toUpperCase()}> "t")`), `[r](${PATH})`);
  assert.equal(
    prepareChatImages(`[the report](file:${ID}) then ![qr](file:${ID})`),
    `[the report](${PATH}) then ![qr](${PATH})`,
  );
});

test("a file:/// link and any other link are left as written, for the Desktop app", () => {
  const unchanged = [
    "[chart](file:///Users/me/chart.png)",
    "[x](file:not-a-uuid)",
    `[x](file:${ID}0)`,
    "[x](https://cdn.example/x.png)",
    `[[FILE:${ID}]]`,
  ];
  for (const text of unchanged) assert.equal(fileSchemeLinksToApi(text), text, text);
});

test("a link to a conversation file's download path names the file", () => {
  assert.equal(chatFileIdFromHref(PATH), ID);
  assert.equal(chatFileIdFromHref(chatFileDownloadPath(ID.toUpperCase())), ID);
  assert.equal(chatFileIdFromHref(`https://evil.example${PATH}`), null);
  assert.equal(chatFileIdFromHref("/api/v1/chat/images/abc.png"), null);
  assert.equal(chatFileIdFromHref(undefined), null);
});
