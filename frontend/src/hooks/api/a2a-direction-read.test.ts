/**
 * The A2A direction read - `yarn test:unit`.
 *
 * Read off the source, like `a2a-verification-read.test.ts`: the hook needs
 * React and the `@/` alias, and `yarn test:unit` has neither. The A2A e2e
 * suite drives the pages.
 *
 * #11790: an inbound integration page called the outbound-only
 * `/a2a/client/{id}` and `/delegations` endpoints and got 404s. The
 * integration now states its direction (`a2a_direction`), and the page asks
 * the verification endpoint only when it does not.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const hook = readFileSync(new URL("./useA2AClientApi.ts", import.meta.url), "utf8");
const start = hook.indexOf("export function useA2ADirection(");
const read = hook.slice(start, start + hook.slice(start).search(/\r?\n\}\r?\n/));
const page = (name: string) =>
  readFileSync(new URL(`../../pages/integration-detail/${name}`, import.meta.url), "utf8");

test("a stated direction answers without asking the outbound-only endpoint", () => {
  assert.notEqual(start, -1);
  assert.match(read, /useA2AVerificationState\(integration\?\.id \?\? "", capable && !stated, options\)/);
  const stated = read.indexOf("if (stated) return stated;");
  const pending = read.indexOf("if (isPending) return undefined;");
  // A disabled query stays pending: the stated answer must come first.
  assert.ok(stated !== -1 && pending > stated);
});

test("the overview badge reads the shared direction, not the verification state", () => {
  const overview = page("OverviewTab.tsx");
  assert.match(overview, /useA2ADirection\(integration\)/);
  assert.doesNotMatch(overview, /useA2AVerificationState/);
});

test("the outbound cards mount for an outbound registration only", () => {
  assert.match(page("ConfigTab.tsx"), /isA2AOutboundCapable && a2aDirection === "outbound" && \(/);
});
