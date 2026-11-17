/**
 * Unit tests for the public deck - `yarn test:unit`.
 *
 * Pinned here: a deck a public link opens reaches the stage as a deck nobody
 * manages and that names nobody, whatever the answer carried, and its pictures
 * load from the token-gated route with the token and the file id escaped.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { PublicPresentation } from "@/types";

import { PUBLIC_DECK_ROUTE, isPublicDeckPath, publicAssetUrl, publicDeckAsPresentation } from "./public-deck.ts";

const DECK = {
  id: "0b8f7c1e-6a2d-4f3b-9c1d-2e5f6a7b8c9d",
  title: "Quarterly Review, Q3 2026",
  theme: "dark",
  aspect_ratio: "16:9",
  kind: "deck",
  slide_count: 1,
  revision: 3,
  spec: { title: "Quarterly Review, Q3 2026", theme: "dark", slides: [{ layout: "title", title: "Q3" }] },
  theme_config: { accent: "#00bcd4" },
  theme_configs: {},
  supported_schemes: ["dark", "light"],
  template_chrome: null,
  asset_meta: {},
  picture_labels: {},
  updated_at: "2026-10-06T10:00:00+00:00",
} as unknown as PublicPresentation;

test("a public deck reaches the stage as a deck nobody manages", () => {
  const deck = publicDeckAsPresentation(DECK);
  assert.equal(deck.can_manage, false);
  assert.equal(deck.permission, null);
  assert.equal(deck.has_public_link, true);
  assert.equal(deck.open_comment_count, 0);
  for (const key of ["user_id", "owner_name", "updated_by_user_id", "updated_by_name", "conversation_id"] as const) {
    assert.equal(deck[key], null, key);
  }
  assert.equal(deck.shared_user_ids, null);
  assert.equal(deck.shared_group_ids, null);
  // What the renderer reads is the answer's own.
  assert.equal(deck.spec, DECK.spec);
  assert.equal(deck.theme_config, DECK.theme_config);
  assert.equal(deck.revision, 3);
});

test("only a public deck page is read without a session", () => {
  assert.equal(isPublicDeckPath("/public/presentations/Ab-_9"), true);
  assert.equal(isPublicDeckPath("/public/presentations"), false);
  assert.equal(isPublicDeckPath("/public/agents"), false);
  assert.equal(isPublicDeckPath("/presentations/0b8f7c1e"), false);
});

test("a public picture loads from the token-gated route, escaped", () => {
  assert.equal(PUBLIC_DECK_ROUTE, "/public/presentations");
  assert.equal(
    publicAssetUrl("/xtm/api/v1", "Ab-_9", "1f2e"),
    "/xtm/api/v1/public/presentations/Ab-_9/assets/1f2e",
  );
  assert.equal(
    publicAssetUrl("/api/v1", "a/b?c", "../x"),
    "/api/v1/public/presentations/a%2Fb%3Fc/assets/..%2Fx",
  );
});
