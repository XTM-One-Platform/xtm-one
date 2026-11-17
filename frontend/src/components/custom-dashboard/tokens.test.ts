/**
 * Unit tests for the page's tokens - `yarn test:unit`.
 *
 * A token is written where it goes (quoted, HTML-escaped, URL-encoded or as
 * is); an element waits for an unset token; computed tokens are arithmetic,
 * text and comparisons on the others, and nothing else ever runs.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { environmentTokens, evaluate, fill, fillOrKeep, namesIn, parse, resolveTokens, resultTokens, tokensFor } from "./tokens.ts";

test("a token is written with its filter, a URL encodes by default", () => {
  const tokens = { q: 'APT "29" <b>', types: ["Malware", "Tool"] };
  assert.equal(fill("Search $q$", tokens), 'Search APT "29" <b>');
  assert.equal(fill("$q|s$", tokens), '"APT \\"29\\" <b>"');
  assert.equal(fill("$q|h$", tokens), "APT &quot;29&quot; &lt;b&gt;");
  assert.equal(fill("/search?q=$q$&t=$types$", tokens, "url"), "/search?q=APT%20%2229%22%20%3Cb%3E&t=Malware%2CTool");
  assert.equal(fill("/x/$q|n$", tokens, "url"), '/x/APT "29" <b>');
  assert.equal(fill("costs $5 and $q$", {}), null);
  assert.equal(fill("costs $5", {}), "costs $5");
});

test("a title never waits: an unset token stays visible", () => {
  assert.equal(fillOrKeep("Findings for $team$ ($n$)", { n: "3" }), "Findings for $team$ (3)");
});

test("names and the tokens a source sends", () => {
  const source = { options: { native: { filters: [{ values: ["$types$", "$q$ x"] }] }, range: "$time$" } };
  assert.deepEqual([...namesIn(source)].sort(), ["q", "time", "types"]);
  assert.deepEqual(tokensFor(source, { types: ["Malware"], q: "", time: "30d", other: "x" }), { time: "30d", types: ["Malware"] });
});

test("the environment and a source's result are tokens", () => {
  assert.deepEqual(environmentTokens({ user: "marcus@example.com", timezone: "Europe/Paris", platform: "" }), {
    "env:user": "marcus@example.com",
    "env:timezone": "Europe/Paris",
  });
  const ok = { status: "ok", fetched_at: "2026-10-25T10:00:00Z", open_url: null } as const;
  assert.deepEqual(resultTokens("kev", { ...ok, shape: "number", data: { value: 37, change: -5 } }, false), {
    "kev:job.status": "done",
    "kev:job.lastUpdated": "2026-10-25T10:00:00Z",
    "kev:result.value": "37",
    "kev:result.change": "-5",
  });
  assert.deepEqual(
    resultTokens("top", { ...ok, shape: "rows", data: { columns: [], rows: [{ name: "VPN", _url: "/x" }], total: 12 } }, false),
    { "top:job.status": "done", "top:job.lastUpdated": "2026-10-25T10:00:00Z", "top:result.name": "VPN", "top:result.count": "12" },
  );
  assert.deepEqual(resultTokens("q", { status: "error", code: "waiting", open_url: null }, false), { "q:job.status": "waiting" });
  assert.deepEqual(resultTokens("q", undefined, true), { "q:job.status": "running" });
});

test("expressions compute tokens from the others", () => {
  const tokens = { n: "4", sev: "high", empty: "", list: ["a", "b"] };
  assert.equal(evaluate("$n$ * 2 + 1", tokens), "9");
  assert.equal(evaluate('if($sev$ == "high", "Critical first", "All")', tokens), "Critical first");
  assert.equal(evaluate('upper($sev$) + "-" + len($sev$)', tokens), "HIGH-4");
  assert.equal(evaluate("round(10 / 3, 2)", tokens), "3.33");
  assert.equal(evaluate('coalesce($missing$, $sev$)', tokens), "high");
  assert.equal(evaluate("$n$ >= 4 && !($sev$ == \"low\")", tokens), "true");
  assert.equal(evaluate("$list$", tokens), "a,b");
  assert.equal(evaluate("$missing$ + 1", tokens), undefined);
  assert.equal(evaluate("1 / 0", tokens), undefined);
});

test("an expression is parsed here and nothing else runs", () => {
  for (const source of ["alert(1)", "this.constructor", "$n$; 1", "a.b", "`x`", "x => 1", "[1]", "1 +", "if(1, 2)", "'x'"]) {
    assert.throws(() => parse(source), String(source));
  }
  assert.throws(() => parse("1+".repeat(200)), /300/);
});

test("the page's tokens: environment wins, computed ones settle a chain", () => {
  const tokens = resolveTokens({
    environment: { "env:user": "marcus" },
    defaults: { sev: "medium", "env:user": "spoofed" },
    set: { sev: "high" },
    expressions: { label: '"Severity " + $sev$', title: '$label$ + " for " + $env:user$' },
    results: {},
  });
  assert.equal(tokens.sev, "high");
  assert.equal(tokens["env:user"], "marcus");
  assert.equal(tokens.title, "Severity high for marcus");
});
