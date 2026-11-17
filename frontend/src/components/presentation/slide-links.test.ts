/**
 * Unit tests for the slide-link reader under a URL path prefix
 * - `yarn test:unit`.
 *
 * Both forms a link takes must resolve under every prefix: the root-relative
 * `/presentations/<id>#/<ref>` the tools write, and the absolute browser URL
 * (origin + prefix + route) the channel resolver produces or a person copies.
 * The prefix `/presentations` is the case that breaks a strip-first reader.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { parsePresentationLinkFrom, parseSlideFragment, resolveSlideRef } from "./slide-links.ts";

const ORIGIN = "https://intranet.example.com";
const DECK = "0b1e4f3a-6d2c-4e8f-9a7b-1c2d3e4f5a6b";

test("the tool's root-relative link resolves under every prefix", () => {
  for (const basePath of ["", "/xtm-one", "/presentations", "/apps/xtm-one"]) {
    const link = parsePresentationLinkFrom(basePath, ORIGIN, `/presentations/${DECK}#/slide-4`);
    assert.deepEqual(link, { presentationId: DECK, slideRef: "slide-4" }, basePath);
  }
});

test("the absolute browser URL under the prefix resolves under every prefix", () => {
  for (const basePath of ["", "/xtm-one", "/presentations", "/apps/xtm-one"]) {
    const href = `${ORIGIN}${basePath}/presentations/${DECK.toUpperCase()}/#/3`;
    const link = parsePresentationLinkFrom(basePath, ORIGIN, href);
    assert.deepEqual(link, { presentationId: DECK, slideRef: "3" }, basePath);
  }
});

test("a link to the deck itself carries no slide ref", () => {
  assert.deepEqual(parsePresentationLinkFrom("/xtm-one", ORIGIN, `/presentations/${DECK}`), {
    presentationId: DECK,
    slideRef: null,
  });
  assert.equal(parsePresentationLinkFrom("", ORIGIN, `/presentations/${DECK}#/`)?.slideRef, "0");
});

test("another host, another route or a sub-page of the viewer is not a slide link", () => {
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, `https://other.example/presentations/${DECK}`), null);
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, `/presentations/${DECK}/speaker`), null);
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, `/reports/${DECK}`), null);
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, "/presentations/not-a-uuid"), null);
  // A link under ANOTHER prefix is not this deployment's viewer.
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, `${ORIGIN}/other/presentations/${DECK}`), null);
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, null), null);
  assert.equal(parsePresentationLinkFrom("/xtm-one", ORIGIN, "http://[bad"), null);
});

test("fragments and refs", () => {
  assert.equal(parseSlideFragment("#/slide-4"), "slide-4");
  assert.equal(parseSlideFragment("#/3/1"), "3");
  assert.equal(parseSlideFragment("#/"), "0");
  assert.equal(parseSlideFragment("#top"), null);
  const spec = { slides: [{ id: "intro" }, { id: "7" }, { id: "close" }] } as never;
  assert.equal(resolveSlideRef(spec, "close"), 2);
  assert.equal(resolveSlideRef(spec, "7"), 1);
  assert.equal(resolveSlideRef(spec, "99"), 2);
  assert.equal(resolveSlideRef(spec, "nope"), null);
});
