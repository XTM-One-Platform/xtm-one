// Settings > Platform Health, read view (#729, #730): the Budget block and the
// Analysis block below it write their caps in the same formats, tokens compact
// and costs in dollars (Sam's design standards s.5 and s.9, one pattern per view).
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { HealthSection } from "@/pages/settings/HealthSection";
import { renderWithProviders } from "@/testing/providers";

const { get, put, post } = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), api: { get, put, post } }));

const settings = {
  enabled: true,
  name: null,
  scope_mode: "all",
  scope_agent_ids: [],
  scope_flow_ids: [],
  budget: { cycle_tokens: 500000, cycle_cost_usd: "5", month_tokens: 10000000, month_cost_usd: "100" },
  analysis: { enabled: true, day_tokens: 100000, day_cost_usd: "1", month_tokens: 3100000, month_cost_usd: "31" },
  schedules: {
    memory: "0 3 * * *",
    quality: "0 4 * * *",
    flows: "0 5 * * *",
    digest: "0 8 * * 1",
    memory_enabled: true,
    quality_enabled: true,
    flows_enabled: true,
    digest_enabled: true,
    timezone: "UTC",
  },
  signals: {
    feedback_enabled: true,
    feedback_drop_points: 10,
    feedback_window_days: 7,
    feedback_min_ratings: 5,
    failure_enabled: true,
    failure_rate_pct: 20,
    failure_last_runs: 10,
    cost_enabled: true,
    cost_increase_pct: 50,
    cost_window_days: 7,
    cost_min_runs: 5,
    model_change_enabled: true,
  },
  proposal_watch_days: 7,
  flow_shadow_days: 7,
  flow_watch_days: 7,
  flow_shadow_min_pairs: 5,
  dry_run: false,
  opencti: {
    connector_stale_minutes: 30,
    connector_errors_max: 10,
    queue_depth_max: 100000,
    search_disk_max_percent: 85,
    ingestion_lag_minutes: 15,
  },
  openaev: { integration_silent_minutes: 60, agents_offline_max_percent: 50 },
  alert_emails: { enabled: false, recipients: [], min_severity: "warning", immediate: true, digest: true },
};

const budget = {
  month: "2026-11",
  caps: settings.budget,
  used: { tokens: 0, cost_usd: "4.2", reserved_tokens: 0, reserved_cost_usd: "0" },
  remaining_tokens: null,
  remaining_cost_usd: null,
  open_cycles: 0,
  refusal: null,
  refusal_message: null,
};

const renderSection = () =>
  renderWithProviders(
    <MemoryRouter>
      <HealthSection />
    </MemoryRouter>,
  );

afterEach(cleanup);

beforeEach(() => {
  get.mockReset().mockImplementation((url: string) => {
    if (url === "/health/settings") return Promise.resolve(settings);
    if (url === "/health/budget") return Promise.resolve(budget);
    if (url === "/health/name") return Promise.resolve({ name: "Platform Health", default_name: "Platform Health" });
    return Promise.resolve([]);
  });
  put.mockReset();
  post.mockReset();
});

describe("HealthSection read view", () => {
  it("writes the Budget caps in the same formats as the Analysis ceilings", async () => {
    renderSection();
    const analysis = await screen.findByTestId("health.analysis");
    // Analysis: tokens compact, costs in dollars.
    expect(analysis).toHaveTextContent("100K");
    expect(analysis).toHaveTextContent("3.1M");
    expect(analysis).toHaveTextContent("$1.00");
    expect(analysis).toHaveTextContent("$31.00");
    // Budget, the block above: the same two formats, never the plain "500,000" or a bare "5".
    const settingsView = screen.getByTestId("settings.health");
    expect(settingsView).toHaveTextContent("500K");
    expect(settingsView).toHaveTextContent("10M");
    expect(settingsView).toHaveTextContent("$5.00");
    expect(settingsView).toHaveTextContent("$100");
    expect(settingsView).not.toHaveTextContent("500,000");
    expect(settingsView).not.toHaveTextContent("10,000,000");
  });

  it("writes the month's spend in dollars, without a trailing USD", async () => {
    renderSection();
    const usage = await screen.findByTestId("health.budget-usage");
    expect(usage).toHaveTextContent("Spent in 2026-11: 0 tokens, $4.20.");
    expect(usage).not.toHaveTextContent("USD");
  });

  it("shows the alert emails block, off by default", async () => {
    renderSection();
    const alerts = await screen.findByTestId("settings.health.alert-emails");
    expect(alerts).toHaveTextContent("Send alert emails");
    expect(alerts).toHaveTextContent("Off");
    expect(screen.queryByTestId("health.read.alert-recipients")).toBeNull();
  });
});
