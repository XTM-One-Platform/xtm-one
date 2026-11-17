/**
 * Unit tests for the public video page's address - `yarn test:unit`.
 *
 * Pinned here: only a page under the public video route is read without a
 * session, so a dead session there never sends its visitor to sign in, while
 * the editor and the library keep their sign-in redirect.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PUBLIC_VIDEO_ROUTE, isPublicVideoPath } from "./public-video.ts";

test("only the public video page reads without a session", () => {
  assert.equal(PUBLIC_VIDEO_ROUTE, "/public/videos");
  assert.equal(isPublicVideoPath("/public/videos/Ab-_9"), true);
  assert.equal(isPublicVideoPath("/public/videos"), false);
  assert.equal(isPublicVideoPath("/public/presentations/Ab-_9"), false);
  assert.equal(isPublicVideoPath("/videos/0b8f7c1e"), false);
});
