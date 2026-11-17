/**
 * Unit tests for the placeholder-path check behind the MCP server form
 * - `yarn test:unit`.
 *
 * A preset whose arguments still carry `/path/to/...` must not be created as
 * is; what is pinned is which arguments count as a placeholder.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { findPlaceholderPath } from "./placeholder-args.ts";

test("a bare placeholder argument is found", () => {
  assert.equal(
    findPlaceholderPath("-y @modelcontextprotocol/server-filesystem /path/to/allowed/dir"),
    "/path/to/allowed/dir",
  );
});

test("a placeholder among other arguments is found", () => {
  assert.equal(
    findPlaceholderPath("--directory /path/to/mcp-security/server/gti/gti_mcp run server.py"),
    "/path/to/mcp-security/server/gti/gti_mcp",
  );
});

test("the first placeholder is the one reported", () => {
  assert.equal(findPlaceholderPath("/path/to/a  /path/to/b"), "/path/to/a");
});

test("a placeholder given as an option value is found", () => {
  assert.equal(findPlaceholderPath("run --directory=/path/to/mcp server.py"), "/path/to/mcp");
  assert.equal(findPlaceholderPath("-d=/path/to/x"), "/path/to/x");
});

test("a quoted placeholder is found, without its quotes", () => {
  assert.equal(findPlaceholderPath(`--root "/path/to/dir"`), "/path/to/dir");
  assert.equal(findPlaceholderPath(`--root='/path/to/dir'`), "/path/to/dir");
});

test("a placeholder assigned to a key is found", () => {
  assert.equal(findPlaceholderPath("ROOT=/path/to/x"), "/path/to/x");
});

test("the bare prefix is a placeholder too", () => {
  assert.equal(findPlaceholderPath("--root /path/to"), "/path/to");
});

test("real paths are not placeholders", () => {
  for (const args of ["/srv/mcp", "~/mcp", "./data", "--directory /home/me/path/to/x run", "/path/tools"]) {
    assert.equal(findPlaceholderPath(args), null, args);
  }
});

test("no arguments means nothing to report", () => {
  assert.equal(findPlaceholderPath(""), null);
  assert.equal(findPlaceholderPath("   "), null);
});
