// The agent's Observability tab: one failing table read stays in that table (its error line and Retry),
// with the app's default `throwOnError: true`, while the rest of the tab keeps rendering.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Component, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Agent } from "@/types";
import { ObservabilityTab } from "./ObservabilityTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

/** Stands for the page boundary: a read that throws past its table lands here. */
class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <p>Failed to load this section</p> : this.props.children;
  }
}

const SUMMARY = {
  trace_count: 3,
  total_cost: 0.3,
  avg_duration_ms: 1200,
  error_rate: 0,
  avg_feedback_score: null,
  quota_consumed: 10,
  avg_quota_per_message: 1,
};

describe("ObservabilityTab", () => {
  it("keeps a failed feedback comments read in its table with Retry", async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/feedback-comments")) return Promise.reject(new Error("boom"));
      if (url.startsWith("/observability/traces/analytics/summary")) return Promise.resolve(SUMMARY);
      if (url.startsWith("/observability/traces/analytics/cost-over-time")) return Promise.resolve({ series: [] });
      if (url.startsWith("/observability/traces/analytics/cost-by-model")) return Promise.resolve({ items: [] });
      if (url.startsWith("/observability/traces?")) return Promise.resolve({ items: [], total: 0 });
      return Promise.resolve({});
    });
    // The app's default (main.tsx): a read that fails throws to the nearest boundary unless it opts out.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true } } });
    renderWithProviders(
      <MemoryRouter>
        <Boundary>
          <ObservabilityTab agent={{ id: "agent-1" } as Agent} />
        </Boundary>
      </MemoryRouter>,
      { client },
    );

    expect(await screen.findByText("Could not load these rows.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.queryByText("Failed to load this section")).toBeNull();
    expect(screen.getByText("Traces (7d)")).toBeTruthy();
  });

  it("draws a long feedback comment on one line, cut at the column width, with a keyboard tooltip", async () => {
    const long = "The answer missed the second indicator and the summary repeated itself several times. ".repeat(4).trim();
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/feedback-comments")) {
        return Promise.resolve({
          items: [
            {
              id: "c1",
              message_id: "m1",
              conversation_id: "conv1",
              channel: "web",
              user_name: "Ann Example",
              user_email: null,
              rating: "negative",
              comment: long,
              created_at: "2026-11-07T09:00:00Z",
              updated_at: "2026-11-07T09:00:00Z",
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        });
      }
      if (url.startsWith("/observability/traces/analytics/summary")) return Promise.resolve(SUMMARY);
      if (url.startsWith("/observability/traces/analytics/cost-over-time")) return Promise.resolve({ series: [] });
      if (url.startsWith("/observability/traces/analytics/cost-by-model")) return Promise.resolve({ items: [] });
      if (url.startsWith("/observability/traces?")) return Promise.resolve({ items: [], total: 0 });
      return Promise.resolve({});
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    renderWithProviders(
      <MemoryRouter>
        <ObservabilityTab agent={{ id: "agent-2" } as Agent} />
      </MemoryRouter>,
      { client },
    );

    const comment = await screen.findByTestId("feedback-comment");
    expect(comment.textContent).toBe(long);
    expect(comment.getAttribute("tabindex")).toBe("0");
    expect(comment.closest("td")).not.toBeNull();
  });

  it("keeps the Rating badge on one line (source guard: a ja label must not wrap per character)", () => {
    // vitest runs from frontend/ (CI and `yarn test`); jsdom computes no Tailwind, so the guard reads the source.
    const source = readFileSync(join(process.cwd(), "src/pages/agent-detail/ObservabilityTab.tsx"), "utf8");
    const rating = source.slice(source.indexOf('key: "rating"'), source.indexOf('key: "comment"'));
    expect(rating).toMatch(/<Badge[^>]*whitespace-nowrap/);
  });
});
