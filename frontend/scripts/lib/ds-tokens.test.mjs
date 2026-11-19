import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveTokens, tokenBlocks, tokenRows } from "./ds-tokens.mjs";

const CSS = `
/* generated */
@theme {
  --color-*: initial;
  --red-400: #f14337;
  --red-700: #881106;
  --gray-100: #f2f2f3;
}
:root {
  --bg: var(--red-700);
  --error: var(--red-400);
  --error-30: color-mix(in srgb, var(--error) 30%, transparent);
}
.light {
  --bg: var(--gray-100);
}
.dark {
  --bg: var(--red-700);
  --loop-a: var(--loop-b);
  --loop-b: var(--loop-a);
}
.layer-0 {
  --bg-layer: var(--bg);
}
@utility text-error {
  color: var(--error);
}
`;

test("the token blocks are read per selector, utilities and comments left out", () => {
  const blocks = tokenBlocks(CSS);
  assert.deepEqual(Object.keys(blocks), ["@theme", ":root", ".light", ".dark", ".layer-0"]);
  assert.equal(blocks[":root"]["--error"], "var(--red-400)");
});

test("each scheme follows the var() chain to the primitive, .light and .dark over :root", () => {
  const resolved = resolveTokens(CSS);
  assert.deepEqual(resolved["--bg"], { light: "#f2f2f3", dark: "#881106" });
  assert.deepEqual(resolved["--error"], { light: "#f14337", dark: "#f14337" });
  assert.equal(resolved["--error-30"].dark, "color-mix(in srgb, #f14337 30%, transparent)");
  assert.equal(resolved["--color-*"], undefined);
  assert.equal(resolved["--bg-layer"], undefined);
});

test("a cycle stays as written instead of looping", () => {
  const resolved = resolveTokens(CSS);
  assert.match(resolved["--loop-a"].dark, /var\(--loop-a\)/);
});

test("rows are filtered by name and sorted", () => {
  const rows = tokenRows(resolveTokens(CSS), /error/);
  assert.deepEqual(
    rows.map((r) => r.token),
    ["--error", "--error-30"],
  );
});
