/**
 * The confirmation of a platform-wide usage reset - `yarn test:unit`.
 *
 * A reset code can name some quota types and windows only. The admin must read
 * exactly the counters the server reset, with what each held before, and never
 * a sentence that would also be true of a reset that cleared everything.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { PlatformQuotaResetCounter } from "../types/index.ts";
import {
  RESET_MESSAGES,
  RESET_PERIOD_LABELS,
  RESET_QUOTA_TYPE_LABELS,
  describePlatformQuotaReset,
  describeResetCounters,
} from "./platform-quota-reset.ts";

/** English catalog behaviour: the id is the message, `{name}` interpolates. */
const t = (id: string, values: Record<string, string | number> = {}) =>
  id.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name]));

const counter = (
  quota_type: PlatformQuotaResetCounter["quota_type"],
  period: PlatformQuotaResetCounter["period"],
  previous_count: number,
): PlatformQuotaResetCounter => ({ quota_type, period, period_key: "2026-10", previous_count });

test("a narrowed reset names its one counter and nothing else", () => {
  const result = { counters: [counter("agentic", "monthly", 100)] };
  assert.deepEqual(describeResetCounters(result, t), ["Agentic Executions, This Month: 100 to 0"]);
  assert.equal(
    describePlatformQuotaReset(result, t),
    "Reset: Agentic Executions, This Month: 100 to 0. Every other counter, each user's own usage and the license limits are unchanged.",
  );
});

test("a default reset lists every counter in the order the server reported them", () => {
  const result = {
    counters: [
      counter("agentic", "daily", 32),
      counter("agentic", "monthly", 100),
      counter("agentic", "yearly", 300),
      counter("sandbox", "daily", 0),
      counter("sandbox", "monthly", 6),
      counter("sandbox", "yearly", 0),
    ],
  };
  assert.deepEqual(describeResetCounters(result, t), [
    "Agentic Executions, Today: 32 to 0",
    "Agentic Executions, This Month: 100 to 0",
    "Agentic Executions, This Year: 300 to 0",
    "Sandbox executions, Today: 0 to 0",
    "Sandbox executions, This Month: 6 to 0",
    "Sandbox executions, This Year: 0 to 0",
  ]);
});

test("the LLM proxy counter has a label of its own", () => {
  assert.deepEqual(describeResetCounters({ counters: [counter("llm_proxy", "yearly", 7)] }, t), [
    "LLM Proxy Requests, This Year: 7 to 0",
  ]);
});

test("every label goes through the translator", () => {
  const seen: string[] = [];
  const recording = (id: string, values?: Record<string, string | number>) => {
    seen.push(id);
    return t(id, values);
  };
  describePlatformQuotaReset({ counters: [counter("sandbox", "daily", 3)] }, recording);
  assert.deepEqual(seen, [
    RESET_QUOTA_TYPE_LABELS.sandbox,
    RESET_PERIOD_LABELS.daily,
    RESET_MESSAGES.counter,
    RESET_MESSAGES.summary,
  ]);
});
