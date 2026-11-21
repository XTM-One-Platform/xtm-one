// An unknown assignment, flow or machine id is an address no page answers: the
// detail page renders Page not found, never the section error, even under the
// app's `throwOnError` default, where a 404 thrown by any read of the page
// would reach the boundary first (#797 S4, UX round 1 of #836: the assignment
// page's runs read 404'd before the page could answer).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import AssignmentDetail from "./AssignmentDetail";
import RunnerDetail from "./RunnerDetail";
import FlowBuilder from "./flow-builder";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// The builder binds palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandHandler: () => {},
}));

const UNKNOWN = "5d1c0a7e-0000-4000-8000-0000000000ff";

/** Every read naming the unknown id answers 404, as the API does; the rest never settles. */
function answerUnknownWith404() {
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (String(path).includes(UNKNOWN)) {
      return Promise.reject(Object.assign(new Error("Not found"), { status: 404 }));
    }
    if (String(path) === "/auth/config") return Promise.resolve({ runners_enabled: true });
    return new Promise(() => {});
  }) as typeof api.get);
}

/** The app's query default (main.tsx): a failed read throws to the page boundary. */
function throwingClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true }, mutations: { retry: false } },
  });
}

function open(url: string) {
  renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/agents/assignments/:id/:tab?" element={<AssignmentDetail />} />
        <Route path="/flows/library/:id" element={<FlowBuilder />} />
        <Route path="/desktop/runners/:id" element={<RunnerDetail />} />
      </Routes>
    </MemoryRouter>,
    { client: throwingClient() },
  );
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  answerUnknownWith404();
});

describe("an unknown id is Page not found", () => {
  it("on the assignment page, whose runs are not read before the assignment answered", async () => {
    open(`/agents/assignments/${UNKNOWN}`);
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    const runsRead = vi.mocked(api.get).mock.calls.some(([path]) => String(path).startsWith(`/assignments/${UNKNOWN}/runs`));
    expect(runsRead).toBe(false);
  });

  it("on the flow builder", async () => {
    open(`/flows/library/${UNKNOWN}`);
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(screen.queryByText("Flow not found")).not.toBeInTheDocument();
  });

  it("on the machine page", async () => {
    open(`/desktop/runners/${UNKNOWN}`);
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(screen.queryByText("Machine not found")).not.toBeInTheDocument();
  });
});
