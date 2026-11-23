// Settings > Platform Health, read view (#729, #730): the Budget block and the
// Analysis block below it write their caps in the same formats, tokens compact
// and costs in dollars (Sam's design standards s.5 and s.9, one pattern per view).
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { HealthSection } from "@/pages/settings/HealthSection";
import { renderWithProviders } from "@/testing/providers";

// XTM One mode: the "Run checks as" block renders (Copilot mode has no module).
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useIsXtmOne: () => true,
}));

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
    if (url === "/health/analysis")
      return Promise.resolve({
        enabled: true,
        ceilings_set: true,
        ceilings: { day_tokens: 100000, day_cost_usd: "1", month_tokens: 3100000, month_cost_usd: "31" },
        day: { tokens: 4763, cost_usd: "0.031852", reserved_tokens: 0 },
        month: { tokens: 9526, cost_usd: "0.063704", reserved_tokens: 0 },
        modules: [],
      });
    if (url === "/health/name") return Promise.resolve({ name: "Platform Health", default_name: "Platform Health" });
    if (url === "/health/settings/spend-limits")
      return Promise.resolve({ locked: true, locked_reason: "environment", env_var: "PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED", limits: [] });
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

  it("shows today's and this month's analysis spend against the ceilings, next to them (#961)", async () => {
    renderSection();
    const spend = await screen.findByTestId("health.analysis-spend");
    expect(screen.getByTestId("health.analysis")).toContainElement(spend);
    expect(spend).toHaveTextContent("Tokens: 4.8K of 100K");
    expect(spend).toHaveTextContent("Tokens: 9.5K of 3.1M");
  });

  it("writes the month's spend in dollars, without a trailing USD", async () => {
    renderSection();
    const usage = await screen.findByTestId("health.budget-usage");
    expect(usage).toHaveTextContent("Spent in 2026-11: 0 tokens, $4.20.");
    expect(usage).not.toHaveTextContent("USD");
  });

  it("is one compact section: a bar jumping to four groups and the pets, each block in its group once", async () => {
    renderSection();
    await screen.findByTestId("health.analysis");
    const bar = screen.getByRole("navigation", { name: "On this page" });
    const links = within(bar).getAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")]);
    expect(links).toEqual([
      ["Status and scope", "#health-settings-status"],
      ["Spend limits", "#health-settings-spend"],
      ["Health checks", "#health-settings-checks"],
      ["Alerts", "#health-settings-alerts"],
      ["Companion pets", "#health-settings-pets"],
    ]);
    expect(within(screen.getByTestId("settings.health.group.spend")).getByTestId("health.analysis")).toBeInTheDocument();
    expect(within(screen.getByTestId("settings.health.group.checks")).getByTestId("settings.health.schedule")).toBeInTheDocument();
    expect(within(screen.getByTestId("settings.health.group.alerts")).getByTestId("settings.health.alert-emails")).toBeInTheDocument();
    // The section title is the tab's; the first block no longer repeats it as a header.
    expect(screen.queryByRole("heading", { name: "Platform Health" })).toBeNull();
  });

  it("moves the bar's current entry with the scroll, the short last groups included (UX r1 and r2 B1)", async () => {
    // r1: the bar observed the pets section alone. r2: an observer band under the header never
    // saw Alerts and Companion pets, which are short and cannot scroll up into it. The bar now
    // reads the groups' positions on every scroll; this drives that geometry, the one the UX
    // Director measured at 1728x1117 (Alerts at 652 px, pets at 814 px at the bottom).
    renderSection();
    await screen.findByTestId("health.analysis");
    const bar = screen.getByRole("navigation", { name: "On this page" });
    const current = () => within(bar).getByRole("link", { current: "location" }).textContent;
    const scroller = (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
    const ids = ["health-settings-status", "health-settings-spend", "health-settings-checks", "health-settings-alerts", "health-settings-pets"];
    const tops = [429, 700, 1500, 2925, 3087];
    let scrollTop = 0;
    const restore: (() => void)[] = [];
    const define = (el: object, key: string, get: () => unknown) => {
      Object.defineProperty(el, key, { configurable: true, get });
      restore.push(() => delete (el as Record<string, unknown>)[key]);
    };
    define(scroller, "scrollTop", () => scrollTop);
    define(scroller, "scrollHeight", () => 3390);
    define(scroller, "clientHeight", () => 1117);
    ids.forEach((id, i) => {
      const el = document.getElementById(id);
      if (!el) throw new Error(`no group ${id}`);
      define(el, "getBoundingClientRect", () => () => ({ top: tops[i] - scrollTop }) as DOMRect);
    });
    const scrollTo = (y: number) => {
      scrollTop = y;
      act(() => {
        document.dispatchEvent(new Event("scroll"));
      });
    };
    try {
      scrollTo(0);
      expect(current()).toBe("Status and scope");
      scrollTo(600);
      expect(current()).toBe("Spend limits");
      scrollTo(1400);
      expect(current()).toBe("Health checks");
      // Over the last viewport the line slides down (UX r3 B1): Alerts gets its own stretch of scroll.
      scrollTo(2060);
      expect(current()).toBe("Alerts");
      // The bottom of the page: the last section, Companion pets.
      scrollTo(2273);
      expect(current()).toBe("Companion pets");
      // A click on Alerts lands at the bottom and stays current while the jump scrolls.
      act(() => {
        within(bar).getByRole("link", { name: "Alerts" }).click();
      });
      scrollTo(2273);
      expect(current()).toBe("Alerts");
      // The reader scrolls back up: the bar follows again, Status and scope at the top.
      act(() => {
        document.dispatchEvent(new Event("wheel"));
      });
      scrollTo(1400);
      expect(current()).toBe("Health checks");
      scrollTo(0);
      expect(current()).toBe("Status and scope");
    } finally {
      restore.forEach((undo) => undo());
    }
  });

  it("locks the Budget and Analysis ceilings and names the variable, the analysis switch kept editable (#1202)", async () => {
    renderSection();
    expect(await screen.findByTestId("health.budget-locked")).toHaveTextContent("PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED");
    expect(screen.getByTestId("health.analysis-locked")).toHaveTextContent("Locked at their defaults");
    act(() => {
      screen.getByRole("button", { name: "Edit" }).click();
    });
    const spend = screen.getByTestId("settings.health.group.spend");
    expect(within(spend).queryAllByRole("spinbutton")).toHaveLength(0);
    expect(within(spend).getByRole("switch", { name: "Analyse the findings" })).toBeInTheDocument();
    expect(within(spend).getByTestId("health.analysis")).toHaveTextContent("100K");
  });

  it("shows a refused run-as user under its field, marked invalid, with no toast (UX r1 B1)", async () => {
    put.mockRejectedValue(
      Object.assign(new Error("server English line"), {
        status: 400,
        detail: { code: "run_as_missing_module_access", modules: ["OpenCRQ"], message: "server English line" },
      }),
    );
    renderSection();
    await screen.findByTestId("health.analysis");
    act(() => {
      screen.getByRole("button", { name: "Edit" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Save" }).click();
    });
    const block = screen.getByTestId("health.run-as");
    expect(await within(block).findByText(/This user has no access to OpenCRQ/)).toBeInTheDocument();
    expect(within(block).getByRole("combobox", { name: "Run checks as" }).getAttribute("aria-invalid")).toBe("true");
    expect(screen.queryByText("Platform Health settings were not saved")).toBeNull();
    expect(screen.queryByText("server English line")).toBeNull();
  });

  it("lands a link to the run-as block on that block once the settings render (UX r1 B2)", async () => {
    const scrolled: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.id);
    };
    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/settings/automations/health#health-settings-run-as"]}>
          <HealthSection />
        </MemoryRouter>,
      );
      await screen.findByTestId("health.run-as");
      expect(scrolled).toContain("health-settings-run-as");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("shows the alert emails block, off by default", async () => {
    renderSection();
    const alerts = await screen.findByTestId("settings.health.alert-emails");
    expect(alerts).toHaveTextContent("Send alert emails");
    expect(alerts).toHaveTextContent("Off");
    expect(screen.queryByTestId("health.read.alert-recipients")).toBeNull();
  });
});
