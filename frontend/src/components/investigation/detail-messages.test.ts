/**
 * Unit tests for the step detail messages - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { DETAIL_MESSAGES, detailMessage } from "./detail-messages.ts";

const NEW_LIST = { resolved: 7, seeds: 7, ips: 4, shared: 2, pivoted: 2 };

test("a list run's seeds read with every parameter, its optional counts aside", () => {
  assert.equal(detailMessage("source.passive_dns_seeds", NEW_LIST), DETAIL_MESSAGES["source.passive_dns_seeds"]);
  assert.equal(
    detailMessage("source.passive_dns_seeds", { ...NEW_LIST, set_aside: 1 }),
    DETAIL_MESSAGES["source.passive_dns_seeds"],
  );
});

test("a list run stored before `ips` and `pivoted` keeps its own text", () => {
  const old = { resolved: 7, seeds: 7, shared: 2 };
  assert.equal(detailMessage("source.passive_dns_seeds", old), null);
  assert.equal(detailMessage("source.passive_dns_seeds", { ...old, set_aside: 1 }), null);
  assert.equal(detailMessage("source.passive_dns_seeds", { ...NEW_LIST, ips: null as unknown as number }), null);
});

test("a message without placeholders needs no parameter, and an unknown code has none", () => {
  assert.equal(detailMessage("run.cancelled", null), "Cancelled");
  assert.equal(detailMessage("source.http_status", null), null);
  assert.equal(detailMessage("source.http_status", { status: 0 }), "HTTP {status}");
  assert.equal(detailMessage(null, NEW_LIST), null);
});
