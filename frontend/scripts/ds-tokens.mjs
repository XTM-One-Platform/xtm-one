#!/usr/bin/env node
/**
 * Print the resolved light and dark value of each design-system token, one
 * tab-separated row per token (`token  light  dark`), from the installed
 * `@filigran/design-system` (scripts/lib/ds-tokens.mjs). Run by the
 * `frontend-ds-tokens` check (.xtm-one/checks.toml) after `frontend-install`.
 *
 * Usage: node scripts/ds-tokens.mjs [PATTERN] [--json]
 *   PATTERN  a regular expression on the token name (`feedback-.*-primary`);
 *            none prints every token.
 *   --json   one JSON object `{ token: { light, dark } }` instead of rows.
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { designSystemDist } from "./fds-migration/design-system-dist.mjs";
import { resolveTokens, tokenRows } from "./lib/ds-tokens.mjs";

const args = process.argv.slice(2);
const json = args.includes("--json");
const pattern = args.find((a) => a !== "--json");

const dist = designSystemDist();
const theme = dist && path.join(dist, "tokens", "theme.css");
if (!theme || !existsSync(theme)) {
  console.error("@filigran/design-system is not installed (dist/tokens/theme.css not found): run frontend-install first.");
  process.exit(2);
}

let filter;
try {
  filter = pattern ? new RegExp(pattern) : undefined;
} catch (error) {
  console.error(`invalid pattern ${pattern}: ${error.message}`);
  process.exit(2);
}

const rows = tokenRows(resolveTokens(readFileSync(theme, "utf8")), filter);
if (json) {
  const object = Object.fromEntries(rows.map((r) => [r.token, { light: r.light, dark: r.dark }]));
  process.stdout.write(`${JSON.stringify(object, null, 1)}\n`);
} else {
  const lines = ["token\tlight\tdark", ...rows.map((r) => `${r.token}\t${r.light}\t${r.dark}`)];
  process.stdout.write(`${lines.join("\n")}\n`);
  console.error(`${rows.length} token(s)${pattern ? ` matching ${pattern}` : ""}`);
}
if (pattern && rows.length === 0) process.exit(1);
