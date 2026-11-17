// A chat card reads its entity by id: once that entity is deleted (or is not
// one the viewer may read), the card says so, and the conversation around it
// stays on screen. Run against the app's client, where every failed query is
// thrown to the nearest ErrorBoundary unless the hook says otherwise.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { useInvestigation } from "./useInvestigationApi";
import { useReport } from "./useReportApi";
import { useWorkItem } from "./useWorkItemApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
}

function refusal(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

type ByIdRead = (id: string) => { isError: boolean };

function Card({ read }: { read: ByIdRead }) {
  const { isError } = read("11111111-2222-3333-4444-555555555555");
  return <p>{isError ? "no longer available" : "loading"}</p>;
}

function renderCard(read: ByIdRead) {
  vi.spyOn(console, "error").mockImplementation(() => {});
  return renderWithProviders(
    <ErrorBoundary fallback={<p>page replaced</p>}>
      <Card read={read} />
    </ErrorBoundary>,
    appQueryClient(),
  );
}

const READS: [string, ByIdRead][] = [
  ["a scheduled work item", useWorkItem],
  ["a report", useReport],
  ["an investigation", useInvestigation],
];

describe("a chat card whose entity is gone", () => {
  it.each(READS)("keeps the conversation when %s answers 404", async (_name, read) => {
    get.mockRejectedValueOnce(refusal(404));
    renderCard(read);
    expect(await screen.findByText("no longer available")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("keeps the conversation when a report the viewer may not read answers 403", async () => {
    get.mockRejectedValueOnce(refusal(403));
    renderCard(useReport);
    expect(await screen.findByText("no longer available")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("still reports a broken server", async () => {
    get.mockRejectedValueOnce(refusal(500));
    renderCard(useWorkItem);
    expect(await screen.findByText("page replaced")).toBeTruthy();
  });
});
