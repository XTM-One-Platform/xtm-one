/**
 * The fleet cost card and the shared limit fields
 * - `yarn test:unit` (vitest + happy-dom). Run against the app's client, where
 * every failed query is thrown to the nearest ErrorBoundary unless the hook
 * says otherwise.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { useIntl } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import { BudgetLimitFields } from "@/components/budget/BudgetLimitFields";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import type { FleetCost } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { limitsDraft } from "@/lib/run-budget-limits";
import { renderWithProviders } from "@/testing/providers";
import type { BudgetLimitKey, BudgetPolicy } from "@/types";

import { FleetCostCard } from "./FleetCostCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: 3, throwOnError: true } } });
}

function cost(traceCount: number): FleetCost {
  return {
    fleet_id: "f-1",
    days: 30,
    since: "2026-10-04T00:00:00Z",
    total_cost: 1.25,
    total_tokens: 1000,
    trace_count: traceCount,
    agents: [],
  };
}

function renderPage(url = "/fleets/f-1") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <ErrorBoundary fallback={<p>page replaced</p>}>
        <p>fleet graph</p>
        <FleetCostCard fleetId="f-1" />
      </ErrorBoundary>
    </MemoryRouter>,
    appQueryClient(),
  );
}

afterEach(() => {
  get.mockReset();
  vi.restoreAllMocks();
});

describe("the fleet cost card", () => {
  it("says a failed read inline and keeps the page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    get.mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    renderPage();
    expect(await screen.findByText("The cost of this fleet could not be read.")).toBeTruthy();
    expect(screen.getByText("fleet graph")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it.each([
    [1, "1 run"],
    [2, "2 runs"],
  ])("counts %i run(s) with a plural", async (count, text) => {
    get.mockResolvedValue(cost(count));
    renderPage();
    expect(await screen.findByText(text)).toBeTruthy();
  });

  it("reads its window from the page's ?window= (#9785), 30 days without it", async () => {
    get.mockResolvedValue(cost(1));
    renderPage("/fleets/f-1?window=7d");
    await screen.findByText("1 run");
    expect(get).toHaveBeenCalledWith("/fleets/f-1/cost?days=7");
    expect(screen.getByRole("radio", { name: "Last 7 days" }).getAttribute("aria-checked")).toBe("true");
    get.mockClear();
    renderPage();
    await screen.findAllByText("1 run");
    expect(get).toHaveBeenCalledWith("/fleets/f-1/cost?days=30");
  });
});

/** The key FleetSettingsPanel and the agent Budget tab pass to `t_i18n`. */
const LIMITS_NEED_ADMIN =
  "{count, plural, one {# limit needs an administrator} other {# limits need an administrator}}";

function LimitsLine({ count }: { count: number }) {
  const intl = useIntl();
  return <p>{intl.formatMessage({ id: LIMITS_NEED_ADMIN, defaultMessage: LIMITS_NEED_ADMIN }, { count })}</p>;
}

describe("the limits that need an administrator", () => {
  it.each([
    [1, "1 limit needs an administrator"],
    [2, "2 limits need an administrator"],
  ])("say %i limit(s) with a plural", (count, text) => {
    renderWithProviders(<LimitsLine count={count} />);
    expect(screen.getByText(text)).toBeTruthy();
  });
});

describe("a limit field above the platform ceiling", () => {
  const policy: BudgetPolicy = {
    can_exceed_ceiling: false,
    ceilings: { max_cost_usd: 5, max_total_tokens: null, max_tool_calls: null, max_duration_seconds: null },
    final_answer_reserve: 0.1,
    fallback_enabled: false,
    fallback_model: null,
  };

  it("is marked invalid, and only that one", () => {
    const draft = { ...limitsDraft(null), max_cost_usd: "9" };
    renderWithProviders(
      <BudgetLimitFields
        draft={draft}
        onChange={() => {}}
        policy={policy}
        blocked={new Set<BudgetLimitKey>(["max_cost_usd"])}
        disabled={false}
      />,
    );
    expect(screen.getByLabelText("Max cost (USD)").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByLabelText("Max tokens").getAttribute("aria-invalid")).not.toBe("true");
  });

  it("names its refusal as the field's description, and only on that field", () => {
    const draft = { ...limitsDraft(null), max_cost_usd: "9" };
    renderWithProviders(
      <BudgetLimitFields
        draft={draft}
        onChange={() => {}}
        policy={policy}
        blocked={new Set<BudgetLimitKey>(["max_cost_usd"])}
        disabled={false}
      />,
    );
    const refusal = "Above the platform budget of $5. Only an administrator can set this.";
    expect(
      screen.getByRole("spinbutton", { name: "Max cost (USD)", description: refusal }),
    ).toBeTruthy();
    expect(
      screen.getByRole("spinbutton", {
        name: "Max tokens",
        description: "Hard ceiling on the tokens one run may consume, prompt and completion together.",
      }),
    ).toBeTruthy();
  });

  it("labels every limit with a real label and an info bubble (UX #9752 B2)", () => {
    renderWithProviders(
      <BudgetLimitFields draft={limitsDraft(null)} onChange={() => {}} policy={policy} blocked={new Set<BudgetLimitKey>()} disabled={false} />,
    );
    for (const name of ["Max cost (USD)", "Max tokens", "Max tool calls", "Max duration (seconds)"]) {
      const field = screen.getByRole("spinbutton", { name });
      expect(document.querySelector(`label[for="${field.id}"]`)).not.toBeNull();
    }
    expect(screen.getAllByRole("img", { name: "About this field" })).toHaveLength(4);
  });
});

describe("the iteration cap row of the run defaults", () => {
  function renderRow(value: string, onChange: (v: string) => void = () => {}) {
    return renderWithProviders(
      <BudgetLimitFields
        draft={limitsDraft(null)}
        onChange={() => {}}
        policy={undefined}
        blocked={new Set<BudgetLimitKey>()}
        disabled={false}
        columns={1}
        iterations={{ value, onChange, inherited: 300, help: "iteration help", info: "iteration info" }}
      />,
    );
  }

  it("is drawn as a fifth row, after the four limits, with what it inherits", () => {
    renderRow("");
    const field = screen.getByLabelText("Max iterations") as HTMLInputElement;
    expect(field.placeholder).toBe("Inherits 300");
    expect(screen.getByRole("spinbutton", { name: "Max iterations", description: "iteration help" })).toBeTruthy();
    const labels = screen.getAllByRole("spinbutton").map((el) => document.getElementById(el.getAttribute("aria-labelledby") ?? "")?.textContent?.trim());
    expect(labels).toEqual(["Max cost (USD)", "Max tokens", "Max tool calls", "Max duration (seconds)", "Max iterations"]);
    expect(screen.queryByRole("button", { name: "Inherit" })).toBeNull();
  });

  it("offers the same Inherit reset as a limit row once a value is set", () => {
    const onChange = vi.fn();
    renderRow("40", onChange);
    screen.getByRole("button", { name: "Inherit" }).click();
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("hands focus to the field when the Inherit reset is used", () => {
    renderRow("40");
    screen.getByRole("button", { name: "Inherit" }).click();
    expect(document.activeElement).toBe(screen.getByLabelText("Max iterations"));
  });
});
