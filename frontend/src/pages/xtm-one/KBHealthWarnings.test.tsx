// The "Indexing stalled" notice sends the reader to configure an embedding
// provider. On Community Edition that setup changes nothing (AI cannot run),
// so the notice stays silent there and the Enterprise upsell notice remains
// the single call to action; it returns the moment AI is licensed.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { KBHealthWarnings } from "./KBHealthWarnings";

const state = vi.hoisted(() => ({ licensed: false, kbs: [] as unknown[], reembedPending: false }));

const QUEUED_KB = {
  id: "kb-1",
  error_count: 0,
  pending_count: 3,
  stale_chunk_count: 0,
  retryable_error_count: 0,
  next_auto_retry_at: null,
  latest_error_code: null,
  latest_error_message: null,
  is_package_origin: false,
};

vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAgenticAIEnabled: () => state.licensed,
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => !state.licensed,
}));
vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: true }),
}));
vi.mock("@/hooks/api/useKnowledgeBaseApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useKnowledgeBaseApi")>()),
  useKnowledgeBases: () => ({ data: state.kbs }),
  useReprocessAllStale: () => ({ mutate: vi.fn(), isPending: state.reembedPending }),
}));
vi.mock("@/hooks/api/useSettingsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useSettingsApi")>()),
  // A reasoning model exists, so only the embedding state is under test.
  useAvailableModels: () => ({ data: [{ id: "m-1" }] }),
  useGeneralAssistantConfig: () => ({ data: { embedding_provider: null, embedding_provider_id: null } }),
}));

function renderWarnings() {
  return renderWithProviders(
    <MemoryRouter>
      <KBHealthWarnings />
    </MemoryRouter>,
  );
}

describe("KBHealthWarnings indexing stalled notice", () => {
  it("stays silent on Community Edition, where configuring a provider changes nothing", () => {
    state.licensed = false;
    state.kbs = [QUEUED_KB];
    renderWarnings();
    expect(screen.queryByText("Indexing stalled: no embedding provider configured")).toBeNull();
    expect(screen.queryByText("Configure AI Models")).toBeNull();
  });

  it("names the missing embedding provider once AI is licensed", () => {
    state.licensed = true;
    state.kbs = [QUEUED_KB];
    renderWarnings();
    expect(screen.getByText("Indexing stalled: no embedding provider configured")).toBeTruthy();
  });
});

describe("KBHealthWarnings stale embeddings list", () => {
  const stale = (id: string, name: string, chunks: number) => ({
    ...QUEUED_KB,
    id,
    name,
    pending_count: 0,
    stale_tuple_count: 1,
    stale_chunk_count: chunks,
  });

  it("lists the stale knowledge bases in the shared data table, most stale chunks first, with search and export", () => {
    state.licensed = true;
    state.kbs = [stale("kb-a", "Alpha", 3), stale("kb-b", "Bravo", 40)];
    renderWarnings();
    fireEvent.click(screen.getByText("Debug: why is this showing?"));
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual(["Bravo", "Alpha"]);
    expect(screen.getByRole("searchbox", { name: "Search knowledge bases..." })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Export CSV/ })).toBeTruthy();
  });

  it("draws the debug panel below the notice row, across its width, never in the copy column beside the actions", () => {
    state.licensed = true;
    state.kbs = [stale("kb-a", "Alpha", 3)];
    renderWarnings();
    expect(document.querySelector('[data-slot="notice-footer"]')).toBeNull();
    fireEvent.click(screen.getByText("Debug: why is this showing?"));
    const footer = document.querySelector('[data-slot="notice-footer"]');
    expect(footer).not.toBeNull();
    const table = screen.getByRole("table");
    expect(footer?.contains(table)).toBe(true);
    // The actions row and the footer are siblings: the table never shares a row with "Re-embed all".
    const reembed = screen.getByRole("button", { name: /Re-embed all/ });
    expect(footer?.contains(reembed)).toBe(false);
    expect(screen.getByTestId("kb-stale-table").contains(table)).toBe(true);
  });

  it("marks Re-embed all busy while the request is pending, and only then", () => {
    state.licensed = true;
    state.kbs = [stale("kb-a", "Alpha", 3)];
    state.reembedPending = false;
    const { unmount } = renderWarnings();
    expect(screen.getByRole("button", { name: /Re-embed all/ }).getAttribute("aria-busy")).toBeNull();
    unmount();
    state.reembedPending = true;
    renderWarnings();
    const pending = screen.getByRole("button", { name: /Re-embed all/ });
    expect(pending.getAttribute("aria-busy")).toBe("true");
    expect((pending as HTMLButtonElement).disabled).toBe(true);
    state.reembedPending = false;
  });
});
