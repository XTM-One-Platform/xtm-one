// Settings > Platform Health > Spend limits, the Module Analyst row (#101 S14d-2,
// dev-docs/platform-health.md 12.5 and 12.7): its ceilings read at rest in the
// same formats as the analysis row, today's and this month's spend on its own
// ledger, an edit written into the draft, an invalid ceiling keeps Save off,
// and the row is absent in Copilot mode (no module there).
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { ModuleAnalystBlock, moduleAnalystValid } from "@/pages/settings/HealthModuleAnalystBlock";
import { renderWithProviders } from "@/testing/providers";
import type { HealthSettings } from "@/types";

const mode = vi.hoisted(() => ({ xtmOne: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useIsXtmOne: () => mode.xtmOne,
}));

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), api: { get, put: vi.fn(), post: vi.fn() } }));

const analyst = {
  enabled: true,
  change_min_interval_minutes: 120,
  excluded_registration_ids: [],
  cycle_tokens: 80000,
  day_tokens: 500000,
  day_cost_usd: "4",
  month_tokens: 10000000,
  month_cost_usd: "60",
};
const draft = { module_analyst: analyst } as unknown as HealthSettings;

/** The spend lock the server reports (#1202); unlocked unless a test locks it. */
let spendLocked = false;

beforeEach(() => {
  spendLocked = false;
  get.mockReset().mockImplementation((url: string) =>
    url === "/health/settings/spend-limits"
      ? Promise.resolve({
          locked: spendLocked,
          locked_reason: spendLocked ? "environment" : null,
          env_var: "PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED",
          limits: [],
        })
      : url === "/health/analysis"
      ? Promise.resolve({
          enabled: false,
          ceilings_set: true,
          ceilings: { day_tokens: 100000, day_cost_usd: "1", month_tokens: 3100000, month_cost_usd: "31" },
          day: { tokens: 0, cost_usd: "0", reserved_tokens: 0 },
          month: { tokens: 0, cost_usd: "0", reserved_tokens: 0 },
          modules: [],
          module_analyst: {
            enabled: true,
            ceilings_set: true,
            ceilings: { cycle_tokens: 80000, day_tokens: 500000, day_cost_usd: "4", month_tokens: 10000000, month_cost_usd: "60" },
            day: { tokens: 68000, cost_usd: "0.32", reserved_tokens: 0 },
            month: { tokens: 136000, cost_usd: "0.64", reserved_tokens: 0 },
          },
        })
      : Promise.resolve([]),
  );
});

afterEach(() => {
  cleanup();
  mode.xtmOne = true;
});

describe("ModuleAnalystBlock", () => {
  it("reads its five ceilings at rest, tokens compact and costs in dollars", () => {
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing={false} onChange={() => {}} />);
    const block = screen.getByTestId("health.module-analyst");
    expect(block).toHaveTextContent("Module Analyst");
    expect(block).toHaveTextContent("80K");
    expect(block).toHaveTextContent("500K");
    expect(block).toHaveTextContent("10M");
    expect(block).toHaveTextContent("$4.00");
    expect(block).toHaveTextContent("$60");
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("lays the ceilings out one period per row, as the analysis grid and the spend lines", async () => {
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing onChange={() => {}} />);
    await screen.findByRole("spinbutton", { name: "Tokens per cycle" });
    const cells = Array.from(screen.getByTestId("health.module-analyst-ceilings").children);
    const names = cells.map((cell) => cell.getAttribute("data-testid") ?? cell.textContent);
    expect(names).toEqual([
      expect.stringContaining("Tokens per cycle"),
      "health.module-analyst-cycle-spacer",
      expect.stringContaining("Tokens per day"),
      expect.stringContaining("Cost per day (USD)"),
      expect.stringContaining("Tokens per month"),
      expect.stringContaining("Cost per month (USD)"),
    ]);
  });

  it("shows today's and this month's spend on its own ledger, against its own ceilings", async () => {
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing={false} onChange={() => {}} />);
    const spend = await screen.findByTestId("health.module-analyst-spend");
    expect(screen.getByTestId("health.module-analyst")).toContainElement(spend);
    expect(spend).toHaveTextContent("Tokens: 68K of 500K");
    expect(spend).toHaveTextContent("Tokens: 136K of 10M");
  });

  it("writes an edited ceiling into the draft, the other fields kept", async () => {
    const onChange = vi.fn();
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing onChange={onChange} />);
    fireEvent.change(await screen.findByRole("spinbutton", { name: "Tokens per cycle" }), { target: { value: "60000" } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ module_analyst: expect.objectContaining({ cycle_tokens: 60000, day_tokens: 500000, enabled: true }) }),
    );
  });

  it("locked (#1202): the ceilings stay read-only in edit mode, the switch stays editable, the variable is named", async () => {
    spendLocked = true;
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing onChange={() => {}} />);
    const notice = await screen.findByTestId("health.module-analyst-locked");
    expect(notice).toHaveTextContent("PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED");
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.getByTestId("health.module-analyst")).toHaveTextContent("$4.00");
    expect(screen.getByRole("switch", { name: "Investigate the modules" })).toBeInTheDocument();
  });

  it("keeps Save off for a negative or fractional token ceiling, and accepts a cleared one", () => {
    expect(moduleAnalystValid(draft)).toBe(true);
    expect(moduleAnalystValid({ module_analyst: { ...analyst, cycle_tokens: 1.5 } } as unknown as HealthSettings)).toBe(false);
    expect(moduleAnalystValid({ module_analyst: { ...analyst, day_cost_usd: "-1" } } as unknown as HealthSettings)).toBe(false);
    expect(moduleAnalystValid({ module_analyst: { ...analyst, month_tokens: null } } as unknown as HealthSettings)).toBe(true);
  });

  it("is absent in Copilot mode", () => {
    mode.xtmOne = false;
    renderWithProviders(<ModuleAnalystBlock draft={draft} editing={false} onChange={() => {}} />);
    expect(screen.queryByTestId("health.module-analyst")).toBeNull();
  });
});
