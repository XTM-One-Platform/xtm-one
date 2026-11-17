/**
 * Unit tests for the trace metadata readers. Run with `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { formatDuration, resolveProviderConfigFromMetadata, traceStatusLabelKey } from "./trace-utils.ts";

test("shows a duration rounded for a person to read, never a float", () => {
  assert.equal(formatDuration(413.11199999999997), "413ms");
  assert.equal(formatDuration(0), "0ms");
  assert.equal(formatDuration(999.6), "1.0s");
  assert.equal(formatDuration(1_234.5), "1.2s");
  assert.equal(formatDuration(90_000), "1.5m");
  assert.equal(formatDuration(null), "-");
  assert.equal(formatDuration(-1), "-");
  assert.equal(formatDuration(Number.NaN), "-");
});

test("shows a known trace status by its catalog key, an unknown one as written", () => {
  assert.equal(traceStatusLabelKey("ok"), "OK");
  assert.equal(traceStatusLabelKey("error"), "Error");
  assert.equal(traceStatusLabelKey("cancelled"), "cancelled");
});

test("reads the provider config that served the trace", () => {
  assert.deepEqual(
    resolveProviderConfigFromMetadata({
      llm_provider: "filigran_gateway",
      llm_provider_config_id: "0f5e",
      llm_provider_config_label: " Team A ",
    }),
    { id: "0f5e", label: "Team A", failover: false, downgraded: false },
  );
});

test("keeps the id when the config has no display name", () => {
  assert.deepEqual(resolveProviderConfigFromMetadata({ llm_provider_config_id: "0f5e" }), {
    id: "0f5e",
    label: null,
    failover: false,
    downgraded: false,
  });
});

test("flags a trace a fallback config answered", () => {
  const config = resolveProviderConfigFromMetadata({
    llm_provider_config_id: "backup",
    llm_provider_failover: [{ provider: "openai", config_id: "backup", label: null }],
  });
  assert.equal(config?.failover, true);
  assert.equal(config?.downgraded, false);
});

test("a primary that served again after a failover is named without the failover mark", () => {
  const config = resolveProviderConfigFromMetadata({
    llm_provider_config_id: "primary",
    llm_provider_failover: [{ provider: "openai", config_id: "backup", label: null }],
  });
  assert.equal(config?.id, "primary");
  assert.equal(config?.failover, false);
});

test("flags a config a budget downgrade moved the run to", () => {
  const config = resolveProviderConfigFromMetadata({
    llm_provider_config_id: "cheap",
    llm_model_downgrade: { from_model: "big", to_model: "small", config_id: "cheap" },
  });
  assert.equal(config?.downgraded, true);
});

test("a failover after the downgrade names the fallback, not the downgrade", () => {
  const config = resolveProviderConfigFromMetadata({
    llm_provider_config_id: "backup",
    llm_model_downgrade: { from_model: "big", to_model: "small", config_id: "cheap" },
    llm_provider_failover: [{ provider: "openai", config_id: "backup", label: null }],
  });
  assert.equal(config?.downgraded, false);
  assert.equal(config?.failover, true);
});

test("returns null for a trace recorded before configs were", () => {
  assert.equal(resolveProviderConfigFromMetadata({ llm_provider: "anthropic" }), null);
  assert.equal(resolveProviderConfigFromMetadata({ llm_provider_config_id: "  " }), null);
  assert.equal(resolveProviderConfigFromMetadata(null), null);
});
