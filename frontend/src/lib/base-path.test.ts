/**
 * Unit tests for the URL path prefix helpers - `yarn test:unit`.
 *
 * The pure half is tested here; `BASE_PATH` itself is read from the document
 * and is `""` under Node, which is also the value every other unit test runs
 * with. The cases mirror `backend/tests/test_base_path.py` so the two
 * normalisations cannot drift.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BASE_PATH,
  joinBasePath,
  normalizeBasePath,
  stripBasePathFrom,
  withBasePath,
} from "./base-path.ts";

test("normalizeBasePath produces one leading slash and no trailing slash", () => {
  assert.equal(normalizeBasePath(null), "");
  assert.equal(normalizeBasePath(""), "");
  assert.equal(normalizeBasePath("   "), "");
  assert.equal(normalizeBasePath("/"), "");
  assert.equal(normalizeBasePath("//"), "");
  assert.equal(normalizeBasePath("xtm-one"), "/xtm-one");
  assert.equal(normalizeBasePath("/xtm-one"), "/xtm-one");
  assert.equal(normalizeBasePath("/xtm-one/"), "/xtm-one");
  assert.equal(normalizeBasePath("  /xtm-one/  "), "/xtm-one");
  assert.equal(normalizeBasePath("//tools//xtm-one/"), "/tools/xtm-one");
});

test("normalizeBasePath reads anything that is not a path as the root instead of throwing", () => {
  for (const raw of [
    "/xtm one",
    "/xtm-one?x=1",
    "/xtm-one#frag",
    "https://host/xtm-one",
    "/../etc",
    "/a/./b",
    // Outside the RFC 3986 unreserved set: percent-encoded, non-ASCII, a
    // quote or a backslash - the backend refuses these at startup, so a meta
    // tag carrying one is not a prefix this app was served under.
    "/xtm%20one",
    "/\u00e9quipe",
    '/xtm"one',
    "/xtm\\one",
    "/xtm<one>",
  ]) {
    assert.equal(normalizeBasePath(raw), "", raw);
  }
});

test("normalizeBasePath accepts every unreserved character", () => {
  assert.equal(normalizeBasePath("/v2.0/xtm_one/~team-1"), "/v2.0/xtm_one/~team-1");
});

test("joinBasePath prefixes only root-relative paths", () => {
  assert.equal(joinBasePath("/xtm-one", "/api/v1/agents"), "/xtm-one/api/v1/agents");
  assert.equal(joinBasePath("/xtm-one", "/"), "/xtm-one/");
  assert.equal(joinBasePath("/xtm-one", "https://other.example/x"), "https://other.example/x");
  assert.equal(joinBasePath("/xtm-one", "//cdn.example/x.js"), "//cdn.example/x.js");
  assert.equal(joinBasePath("/xtm-one", "data:image/png;base64,AAAA"), "data:image/png;base64,AAAA");
  assert.equal(joinBasePath("/xtm-one", "relative/path"), "relative/path");
  assert.equal(joinBasePath("/xtm-one", "#/slide-3"), "#/slide-3");
  assert.equal(joinBasePath("/xtm-one", ""), "");
});

test("joinBasePath at the root is the identity", () => {
  for (const path of ["/api/v1/agents", "/", "https://other.example/x", "relative"]) {
    assert.equal(joinBasePath("", path), path);
  }
});

test("joinBasePath leaves every non-path URL alone", () => {
  for (const url of ["mailto:x@y.example", "blob:https://host/abc", "tel:+1", "javascript:void(0)", "?q=1"]) {
    assert.equal(joinBasePath("/xtm-one", url), url, url);
  }
});

test("joinBasePath is deliberately not idempotent", () => {
  // A guard skipping inputs that already start with the prefix would leave a
  // legitimate `/tools/...` route unprefixed on a deployment served at `/tools`.
  assert.equal(joinBasePath("/tools", "/tools/123"), "/tools/tools/123");
  assert.equal(joinBasePath("/xtm-one", "/xtm-one/agents"), "/xtm-one/xtm-one/agents");
});

test("stripBasePathFrom is the inverse of joinBasePath for pathnames under the prefix", () => {
  assert.equal(stripBasePathFrom("/xtm-one", "/xtm-one/presentations/abc"), "/presentations/abc");
  assert.equal(stripBasePathFrom("/xtm-one", "/xtm-one/"), "/");
  assert.equal(stripBasePathFrom("/xtm-one", "/xtm-one"), "/");
  assert.equal(stripBasePathFrom("/xtm-one", "/xtm-one-two/x"), "/xtm-one-two/x");
  assert.equal(stripBasePathFrom("/xtm-one", "/presentations/abc"), "/presentations/abc");
  assert.equal(stripBasePathFrom("", "/presentations/abc"), "/presentations/abc");
  // Multi-segment prefix, case sensitivity, an empty pathname.
  assert.equal(stripBasePathFrom("/tools/xtm-one", "/tools/xtm-one/agents"), "/agents");
  assert.equal(stripBasePathFrom("/xtm-one", "/XTM-ONE/agents"), "/XTM-ONE/agents");
  assert.equal(stripBasePathFrom("/xtm-one", ""), "");
  for (const path of ["/", "/a", "/a/b?x=1#y"]) {
    assert.equal(stripBasePathFrom("/xtm-one", joinBasePath("/xtm-one", path)), path, path);
  }
});

test("without a document the page prefix is the root and withBasePath is the identity", () => {
  assert.equal(BASE_PATH, "");
  assert.equal(withBasePath("/api/v1/agents"), "/api/v1/agents");
});

test("the page prefix is read once from the meta tag the backend writes", async () => {
  // A fresh module instance (the query string defeats the ESM cache) evaluated
  // against a stubbed document carrying the tag the branded index.html has.
  const scope = globalThis as { document?: unknown; window?: unknown };
  scope.document = {
    querySelector: (selector: string) =>
      selector === 'meta[name="base-path"]' ? { getAttribute: () => "/xtm-one/" } : null,
  };
  scope.window = { location: { origin: "https://intranet.example.com" } };
  try {
    const specifier = "./base-path.ts?with-meta";
    const fresh: typeof import("./base-path.ts") = await import(specifier);
    assert.equal(fresh.BASE_PATH, "/xtm-one");
    assert.equal(fresh.withBasePath("/api/v1/agents"), "/xtm-one/api/v1/agents");
    assert.equal(fresh.stripBasePath("/xtm-one/agents/1"), "/agents/1");
    assert.equal(fresh.absoluteUrl("/presentations/abc"), "https://intranet.example.com/xtm-one/presentations/abc");
    assert.equal(fresh.platformOrigin(), "https://intranet.example.com/xtm-one");
  } finally {
    delete scope.document;
    delete scope.window;
  }
});
