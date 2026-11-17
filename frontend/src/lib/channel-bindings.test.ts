// Continuing a conversation in Slack or Teams refreshes every conversation a
// binding can move away from.

import { test } from "node:test";
import assert from "node:assert/strict";

import { QueryClient } from "@tanstack/react-query";

import { refreshChannelContinuation } from "./channel-bindings.ts";

test("a Teams personal chat re-pointed elsewhere refreshes the conversation it left", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity, retry: false } } });
  const keys = [
    ["conversation", "now-continued"],
    ["conversation", "continued-until-now"],
    ["conversation-channel-bindings", "now-continued"],
    ["conversation-channel-bindings", "continued-until-now"],
    ["conversations", { scope: "own" }],
  ];
  for (const key of keys) client.setQueryData(key, { seeded: true });

  await refreshChannelContinuation(client);

  const invalidated = (key: unknown[]) => client.getQueryState(key)?.isInvalidated ?? false;
  for (const key of keys.slice(0, 4)) assert.equal(invalidated(key), true, JSON.stringify(key));
  // The list shows where a conversation started, never where it is continued.
  assert.equal(invalidated(keys[4]), false);
});
