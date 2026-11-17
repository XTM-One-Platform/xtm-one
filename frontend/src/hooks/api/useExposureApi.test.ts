/**
 * The exposure readings are optional sections - `yarn test:unit`.
 *
 * The "Sharing and access" card and the "Shared - N ways" badges render
 * nothing when their reading fails. The app-wide `throwOnError: true`
 * (main.tsx) would instead hand a 404, a 403 or a 5xx to the page's
 * ErrorBoundary, and an integration or MCP server overview, or a whole list
 * page, would become "Failed to load this section".
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const sources: Record<string, string> = {
  "useExposureApi.ts": readFileSync(new URL("./useExposureApi.ts", import.meta.url), "utf8"),
};

function queryOptions(file: string, hook: string): string {
  const source = sources[file];
  const start = source.indexOf(`export function ${hook}(`);
  assert.notEqual(start, -1, `${hook} is defined in ${file}`);
  const end = source.indexOf("\n}\n", start);
  return source.slice(start, end);
}

const OPTIONAL_READS: Array<[string, string]> = [
  ["useExposureApi.ts", "useResourceExposure"],
  ["useExposureApi.ts", "useResourceExposureSummary"],
];

for (const [file, hook] of OPTIONAL_READS) {
  test(`${hook} never takes the page down`, () => {
    assert.match(queryOptions(file, hook), /throwOnError:\s*false/);
  });

  test(`${hook} answers a refusal at once, without retrying`, () => {
    assert.match(queryOptions(file, hook), /retry:\s*false/);
  });
}

test("the preview is marked read-only, so it does not refresh the exposure reads it answers", () => {
  assert.match(queryOptions("useExposureApi.ts", "useResourceExposurePreview"), /readOnly:\s*true/);
});
