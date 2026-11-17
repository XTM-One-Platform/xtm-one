// The OpenRouter model catalog is drawn by the shared DataTable
// (dev-docs/reference/data-tables.md, slice 7 of #9378): search on name and id,
// Model author / Capabilities / Offered filters, sort on every column (name
// first), Export CSV, the state kept in the URL (orm_), the providers of a model
// in its opened row, and the offer switch and default star kept per row.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Integration, OpenRouterEndpoint, OpenRouterModel } from "@/types";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { ENDPOINT_COLUMN_HEADERS, ENDPOINT_COLUMN_WIDTHS, MODEL_COLUMN_WIDTHS, OpenRouterModelsTab } from "./OpenRouterModelsTab";

const model = (id: string, overrides: Partial<OpenRouterModel> = {}): OpenRouterModel => ({
  id,
  name: id,
  author: id.split("/")[0],
  created: "2026-01-01T00:00:00+00:00",
  description: null,
  context_length: 128_000,
  max_completion_tokens: null,
  is_moderated: false,
  input_modalities: ["text"],
  output_modalities: ["text"],
  pricing: { prompt: 1, completion: 2 } as OpenRouterModel["pricing"],
  variable_pricing: false,
  is_free: false,
  supports_tools: false,
  supports_reasoning: false,
  supports_structured_outputs: false,
  supported_parameters: [],
  knowledge_cutoff: null,
  expiration_date: null,
  ...overrides,
});

const MODELS = [
  model("openai/gpt-4o", { name: "OpenAI: GPT-4o", supports_tools: true }),
  model("anthropic/claude-sonnet-5", { name: "Anthropic: Claude Sonnet 5", supports_tools: true, supports_reasoning: true }),
  model("meta-llama/llama-3.3-70b-instruct:free", { name: "Llama 3.3 70B (free)", is_free: true }),
];

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const AUTH = { isAdmin: true };

vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => AUTH,
}));

const ANNOUNCEMENT_STATUS = { data: undefined, refetch: vi.fn() };

vi.mock("@/hooks/api/useSettingsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useSettingsApi")>()),
  useModelAnnouncementStatus: () => ANNOUNCEMENT_STATUS,
}));

const mutation = { mutate: vi.fn(), isPending: false };

// Stable answers, as a query cache gives: a new provider object per render would
// restart the selection effect of the tab on every render.
const OVERVIEW = {
  isLoading: false,
  error: null,
  data: {
    llm_providers: [{ id: "p-1", display_name: "OpenRouter", available_models: ["openai/gpt-4o"], default_model: "openai/gpt-4o" }],
    can_link_provider: true,
    link_refusal: null,
    adoptable_providers: [],
  },
};
const CATALOG = { isLoading: false, error: null, data: { models: MODELS } };
const ENDPOINTS: { isLoading: boolean; error: null; data: { endpoints: OpenRouterEndpoint[] } } = {
  isLoading: false,
  error: null,
  data: { endpoints: [] },
};
const endpoint = (provider_name: string, supports_tools: boolean): OpenRouterEndpoint => ({
  provider_name,
  tag: null,
  quantization: null,
  context_length: 128_000,
  max_completion_tokens: null,
  status: 0,
  uptime_last_30m: 99.5,
  uptime_last_1d: null,
  latency_p50_ms: 420,
  throughput_p50: null,
  supports_implicit_caching: false,
  supports_tools,
  pricing: { prompt: 1, completion: 2, input_cache_read: null },
});

vi.mock("@/hooks/api/useOpenRouterApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOpenRouterApi")>()),
  useOpenRouterOverview: () => OVERVIEW,
  useOpenRouterModels: () => CATALOG,
  useOpenRouterModelEndpoints: () => ENDPOINTS,
  useLinkOpenRouterProvider: () => mutation,
  useUpdateOpenRouterProviderModels: () => mutation,
  useUnlinkOpenRouterProvider: () => mutation,
}));

const INTEGRATION = { id: "i-1", name: "OpenRouter" } as unknown as Integration;

function renderModels() {
  return renderWithProviders(
    <MemoryRouter>
      <OpenRouterModelsTab integration={INTEGRATION} />
    </MemoryRouter>,
  );
}

function names(): string[] {
  return screen.getAllByTestId("openrouter-model-row").map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

describe("OpenRouter model catalog table", () => {
  // The visible columns are kept per user: a column hidden in one test stays hidden in the next.
  afterEach(() => localStorage.clear());

  it("lists the catalog in the shared data table, by name, with Export CSV", () => {
    renderModels();
    expect(names().map((text) => text.split("/")[0])).toEqual([
      "Anthropic: Claude Sonnet 5anthropic",
      "Llama 3.3 70B (free)meta-llama",
      "OpenAI: GPT-4oopenai",
    ]);
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
  });

  it("narrows the rows through the table search on name and id", () => {
    vi.useFakeTimers();
    try {
      renderModels();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "claude" } });
      act(() => {
        vi.runAllTimers();
      });
      expect(screen.getAllByTestId("openrouter-model-row")).toHaveLength(1);
      expect(names()[0]).toContain("Claude Sonnet 5");
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers the author, capability and offered filters as pills", () => {
    renderModels();
    for (const label of ["Model author", "Capabilities", "Offered"]) {
      expect(screen.getAllByRole("button", { name: new RegExp(label) }).length).toBeGreaterThan(0);
    }
  });

  it("lays out fixed with a width on every drawn column", () => {
    renderModels();
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    const widths = screen.getAllByRole("columnheader").map((th) => th.style.width);
    expect(widths).toEqual(
      [
        MODEL_COLUMN_WIDTHS.name,
        MODEL_COLUMN_WIDTHS.capabilities,
        MODEL_COLUMN_WIDTHS.figure,
        MODEL_COLUMN_WIDTHS.figure,
        MODEL_COLUMN_WIDTHS.figure,
        MODEL_COLUMN_WIDTHS.date,
        MODEL_COLUMN_WIDTHS.offered,
      ].map((px) => `${px}px`),
    );
  });

  // Standard v2 (slice 7e of #9378): the name and the model id are the two lines of one
  // row height, and every figure column can be hidden from Columns.
  it("draws two-line rows and hides a figure column from Columns", () => {
    renderModels();
    const gpt = screen.getAllByTestId("openrouter-model-row").find((row) => row.textContent?.includes("GPT-4o"));
    expect(within(gpt as HTMLElement).getByText("OpenAI: GPT-4o")).toBeInTheDocument();
    expect(within(gpt as HTMLElement).getByText("openai/gpt-4o")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Context/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    fireEvent.click(within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Context" }));
    expect(screen.queryByRole("columnheader", { name: /Context/ })).toBeNull();
  });

  it("keeps the offer switch and the default star on each row", () => {
    renderModels();
    const switches = screen.getAllByRole("switch", { name: "Offer this model on the platform" });
    expect(switches).toHaveLength(3);
    const gpt = screen.getAllByTestId("openrouter-model-row").find((row) => row.textContent?.includes("GPT-4o"));
    expect(within(gpt as HTMLElement).getByRole("switch")).toBeChecked();
    expect(within(gpt as HTMLElement).getByRole("button", { name: "Default model of the provider" })).toHaveAttribute("aria-pressed", "true");
  });

  it("opens a model's providers in the row below it", () => {
    renderModels();
    expect(screen.queryByText("No provider serves this model right now.")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "Show providers" })[0]);
    expect(screen.getByTestId("data-table-expanded-row")).toBeInTheDocument();
    expect(screen.getByText("No provider serves this model right now.")).toBeInTheDocument();
  });

  it("keeps a long provider name and its tool icon on one line (one row height)", () => {
    ENDPOINTS.data.endpoints = [endpoint("Lab Provider A with a long name", true), endpoint("Lab Provider B", false)];
    try {
      renderModels();
      fireEvent.click(screen.getAllByRole("button", { name: "Show providers" })[0]);
      const expanded = screen.getByTestId("data-table-expanded-row");
      const table = within(expanded).getByRole("table");
      expect(table).toHaveAttribute("data-layout", "fixed");
      const header = within(table).getByRole("columnheader", { name: /Provider/ });
      expect(header).toHaveStyle({ width: `${ENDPOINT_COLUMN_WIDTHS.provider}px` });
      const cells = within(expanded).getAllByTestId("openrouter-endpoint-provider");
      expect(cells).toHaveLength(2);
      expect(cells[0]).toHaveTextContent("Lab Provider A with a long name");
      expect(cells[1]).toHaveTextContent("Lab Provider B");
      expect(within(cells[0]).getByRole("img", { name: "Calls tools" })).toBeInTheDocument();
      expect(within(cells[1]).queryByRole("img", { name: "Calls tools" })).toBeNull();
    } finally {
      ENDPOINTS.data.endpoints = [];
    }
  });

  // UX round 2 of #10686, B1: "Disponibilité 30 min" ran past its 120 px column. A header is
  // never cut (whitespace-nowrap, no tooltip), so each column holds its widest translation.
  // Upper-bound estimate of the rendered header at the 12 px table font: 6.5 px per Latin
  // character, 12 px per wide (CJK, full-width) character, 16 px for the sort icon and its
  // gap, 32 px of cell padding (measured: 139 px for the fr uptime label with its icon).
  it("fits every providers header in its column in en, fr, es and ja", () => {
    const estimate = (text: string) =>
      [...text].reduce((total, char) => total + (/[\u3000-\u9fff\uff00-\uffef]/.test(char) ? 12 : 6.5), 0) + 16 + 32;
    const catalogs: Record<string, Record<string, string>> = { en, fr, es, ja };
    for (const [key, header] of Object.entries(ENDPOINT_COLUMN_HEADERS)) {
      const width = ENDPOINT_COLUMN_WIDTHS[key as keyof typeof ENDPOINT_COLUMN_WIDTHS];
      for (const [locale, catalog] of Object.entries(catalogs)) {
        const text = catalog[header];
        expect(text, `${locale} translation of "${header}"`).toBeTruthy();
        expect(estimate(text), `${locale} "${text}" in the ${width} px ${key} column`).toBeLessThanOrEqual(width);
      }
    }
  });
});
