// The activity per model of the OpenRouter usage page is drawn by the shared
// DataTable (dev-docs/reference/data-tables.md, slice 7): search on model and
// provider, a Providers filter, sort on every column (most spent first), page
// size, Export CSV, and the state kept in the URL (orm_).

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, OpenRouterActivityRow } from "@/types";
import { MODEL_COLUMN_WIDTH, OpenRouterUsageTab } from "./OpenRouterUsageTab";

const row = (overrides: Partial<OpenRouterActivityRow>): OpenRouterActivityRow => ({
  date: "2026-09-30",
  model: "openai/gpt-4o",
  provider_name: "OpenAI",
  usage: 0,
  byok_usage_inference: 0,
  requests: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  reasoning_tokens: 0,
  cached_tokens: 0,
  ...overrides,
});

const ROWS = [
  row({ model: "openai/gpt-4o", provider_name: "OpenAI", usage: 1, requests: 2 }),
  row({ model: "anthropic/claude-sonnet-5", provider_name: "Anthropic", usage: 1330.42, requests: 5624, prompt_tokens: 20_234_567 }),
  row({ model: "mistral/mistral-large", provider_name: "Mistral", usage: 0.5, requests: 4 }),
];

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

vi.mock("@/components/ui/charts/cost-over-time-chart", () => ({ default: () => null }));

vi.mock("@/hooks/api/useOpenRouterApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOpenRouterApi")>()),
  useOpenRouterOverview: () => ({
    isLoading: false,
    error: null,
    data: {
      key: null,
      key_error: null,
      credits: null,
      credits_error: null,
      has_api_key: false,
      has_management_key: true,
    },
  }),
  useOpenRouterActivity: () => ({ isLoading: false, error: null, data: { rows: ROWS } }),
}));

const INTEGRATION = { id: "i-1" } as unknown as Integration;

function renderUsage() {
  return renderWithProviders(
    <MemoryRouter>
      <OpenRouterUsageTab integration={INTEGRATION} />
    </MemoryRouter>,
  );
}

function modelCells(): string[] {
  const table = screen.getByRole("table");
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getAllByRole("cell")[0].textContent ?? "");
}

describe("OpenRouter activity per model", () => {
  // The visible columns are kept per user: a column hidden in one test stays hidden in the next.
  afterEach(() => localStorage.clear());

  it("lists the models in the shared data table, most spent first, with Export CSV", () => {
    renderUsage();
    expect(modelCells()).toEqual(["anthropic/claude-sonnet-5", "openai/gpt-4o", "mistral/mistral-large"]);
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
  });

  it("narrows the rows by provider through the table search", () => {
    vi.useFakeTimers();
    try {
      renderUsage();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "mistral" } });
      act(() => {
        vi.runAllTimers();
      });
      expect(modelCells()).toEqual(["mistral/mistral-large"]);
    } finally {
      vi.useRealTimers();
    }
  });

  // UX r1 B1 on #10550: a fixed layout keeps the Model column at its floor, so the
  // figure columns never take the width of the model names.
  it("lays out fixed with a width floor on the Model column", () => {
    renderUsage();
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    expect(screen.getAllByRole("columnheader")[0].style.width).toBe(`${MODEL_COLUMN_WIDTH}px`);
  });

  // Standard v2 (slice 7e of #9378): the figure columns can be hidden from Columns.
  it("hides a figure column from Columns", () => {
    renderUsage();
    expect(screen.getByRole("columnheader", { name: /Input tokens/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    fireEvent.click(within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Input tokens" }));
    expect(screen.queryByRole("columnheader", { name: /Input tokens/ })).toBeNull();
  });

  // UX r1 B2 on #10550: requests, tokens and spend are compact, the exact value on hover.
  it("draws compact figures with the exact value on hover", () => {
    renderUsage();
    const cells = within(screen.getAllByRole("row")[1]).getAllByRole("cell");
    const figures = cells.map((c) => c.textContent ?? "");
    expect(figures).toContain("5.6K");
    expect(figures).toContain("20.2M");
    expect(figures).toContain("$1.3K");
    expect(screen.getAllByTitle("5,624").length).toBeGreaterThan(0);
    expect(screen.getAllByTitle("$1,330.42").length).toBeGreaterThan(0);
  });
});
