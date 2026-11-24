// Two former addresses retired by #797 render Page not found (#822), found by
// the production retest of #799: `/observability/traces/sessions` was read as
// a trace id (GET /traces/sessions answers a session list, and the trace page
// crashed on it), and `/profile/runners` rendered an empty profile.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import Profile from "./Profile";
import TraceDetail from "./TraceDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

function open(url: string) {
  renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/observability/traces/:id/:tab?" element={<TraceDetail />} />
        <Route path="/profile/:tab?" element={<Profile />} />
      </Routes>
    </MemoryRouter>,
  );
}

const requestedLike = (pattern: RegExp) => vi.mocked(api.get).mock.calls.some(([called]) => pattern.test(String(called)));

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation((() => new Promise(() => {})) as typeof api.get);
});

describe("former addresses", () => {
  it("a trace segment that is not a trace id is not found and never looked up", async () => {
    open("/observability/traces/sessions");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(requestedLike(/^\/traces\/sessions/)).toBe(false);
  });

  it("a trace id is looked up", () => {
    open("/observability/traces/5d1c0a7e-0000-4000-8000-000000000001");
    expect(screen.queryByText("Page not found")).not.toBeInTheDocument();
    expect(requestedLike(/^\/traces\/5d1c0a7e-0000-4000-8000-000000000001$/)).toBe(true);
  });

  it("the former runners tab of the profile is not found", async () => {
    open("/profile/runners");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("a profile tab is not refused", () => {
    open("/profile/memory");
    expect(screen.queryByText("Page not found")).not.toBeInTheDocument();
  });
});
