// The API keys of the OpenRouter console are drawn by the shared DataTable
// (dev-docs/reference/data-tables.md, slice 7 of #9378): search on name and
// label, Status and Used by filters, sort on every column (name first), Export
// CSV, the state kept in the URL (ork_), and the edit / delete row actions.

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, OpenRouterApiKey } from "@/types";
import { KEY_COLUMN_WIDTHS, KEY_NAME_COLUMN_WIDTH, OpenRouterKeysTab } from "./OpenRouterKeysTab";

const key = (overrides: Partial<OpenRouterApiKey>): OpenRouterApiKey => ({
  hash: "h",
  name: "key",
  label: "sk-or-v1-abc...xyz",
  disabled: false,
  limit: null,
  limit_remaining: null,
  limit_reset: null,
  include_byok_in_limit: false,
  usage: 0,
  usage_daily: 0,
  usage_weekly: 0,
  usage_monthly: 0,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: null,
  expires_at: null,
  in_use: [],
  ...overrides,
});

const KEYS = [
  key({ hash: "h-prod", name: "Production", usage: 1330.42, usage_monthly: 120, in_use: ["llm_provider", "integration"] }),
  key({ hash: "h-ci", name: "CI pipeline", disabled: true, limit: 50, limit_remaining: 12 }),
  key({ hash: "h-dev", name: "Development", usage: 3 }),
];

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const mutation = { mutate: vi.fn(), isPending: false };

vi.mock("@/hooks/api/useOpenRouterApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOpenRouterApi")>()),
  useOpenRouterOverview: () => ({
    isLoading: false,
    error: null,
    data: { has_management_key: true, can_manage_keys: true },
  }),
  useOpenRouterKeys: () => ({ isLoading: false, error: null, data: { keys: KEYS, truncated: false } }),
  useCreateOpenRouterKey: () => mutation,
  useUpdateOpenRouterKey: () => mutation,
  useDeleteOpenRouterKey: () => mutation,
}));

const INTEGRATION = { id: "i-1" } as unknown as Integration;

function renderKeys(locale = "en") {
  return renderWithProviders(
    <MemoryRouter>
      <OpenRouterKeysTab integration={INTEGRATION} />
    </MemoryRouter>,
    { locale },
  );
}

function nameCells(): string[] {
  return within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getAllByRole("cell")[0].textContent ?? "");
}

describe("OpenRouter API keys table", () => {
  it("lists the keys in the shared data table, by name, with Export CSV", () => {
    renderKeys();
    expect(nameCells().map((text) => text.split("sk-or")[0])).toEqual(["CI pipeline", "Development", "Production"]);
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
  });

  it("narrows the rows through the table search", () => {
    vi.useFakeTimers();
    try {
      renderKeys();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "prod" } });
      act(() => {
        vi.runAllTimers();
      });
      expect(nameCells().map((text) => text.split("sk-or")[0])).toEqual(["Production"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lays out fixed with a width floor on the Name column", () => {
    renderKeys();
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    expect(screen.getAllByRole("columnheader")[0].style.width).toBe(`${KEY_NAME_COLUMN_WIDTH}px`);
  });

  it("gives every column a width so no cell wraps on a narrow screen (UX round 1 on #10601, B1)", () => {
    renderKeys();
    const widths = screen.getAllByRole("columnheader").slice(0, 8).map((th) => th.style.width);
    expect(widths).toEqual(
      [
        KEY_NAME_COLUMN_WIDTH,
        KEY_COLUMN_WIDTHS.used_by,
        KEY_COLUMN_WIDTHS.money,
        KEY_COLUMN_WIDTHS.money,
        KEY_COLUMN_WIDTHS.limit,
        KEY_COLUMN_WIDTHS.date,
        KEY_COLUMN_WIDTHS.date,
        KEY_COLUMN_WIDTHS.enabled,
      ].map((px) => `${px}px`),
    );
  });

  it("draws one Used by badge and counts the others as +N", () => {
    renderKeys();
    const uses = screen.getByTestId("openrouter-key-uses");
    expect(within(uses).getByText("Platform LLM provider")).toBeInTheDocument();
    expect(within(uses).queryByText("This integration")).toBeNull();
    expect(within(uses).getByText("+1")).toBeInTheDocument();
  });

  it("writes the dates in the user's locale (UX round 2 on #10601, B1)", () => {
    renderKeys("fr");
    const row = screen.getAllByTestId("openrouter-key-row")[0];
    expect(row.textContent).toContain("oct. 2026");
    expect(row.textContent).not.toContain("Oct 1, 2026");
  });

  it("keeps the edit and delete actions, delete locked on a key the platform runs on", () => {
    renderKeys();
    expect(screen.getAllByRole("button", { name: "Edit API key" })).toHaveLength(3);
    const prod = screen.getAllByTestId("openrouter-key-row").find((r) => r.textContent?.includes("Production"));
    expect(prod).toBeDefined();
    expect(within(prod as HTMLElement).getByRole("button", { name: "Delete API key" })).toBeDisabled();
    expect(within(prod as HTMLElement).getByText("Platform LLM provider")).toBeInTheDocument();
  });
});
