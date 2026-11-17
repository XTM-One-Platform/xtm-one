// An unknown or deleted workspace or investigation pack is an answer, not a
// fault: its 404 must reach the page, which
// shows NotFoundState, instead of the ErrorBoundary. A 5xx still throws.
// Run against the app's client, where every failed query is thrown to the
// nearest ErrorBoundary unless the hook says otherwise.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { useCtemCase } from "./useCtemCasesApi";
import { useInvestigationPack, useInvestigationPacks, usePackBuilderCatalog } from "./useInvestigationPackApi";
import { useWorkspace } from "./useWorkspaceApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
}

function failure(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

function WorkspacePage() {
  const workspace = useWorkspace("unknown-id");
  if (workspace.isLoading) return <p>loading</p>;
  return workspace.data ? <p>found</p> : <p>Workspace not found</p>;
}

function PackPage() {
  const pack = useInvestigationPack("unknown-id");
  if (pack.isLoading) return <p>loading</p>;
  return pack.data ? <p>found</p> : <p>Investigation pack not found</p>;
}

function PackListPage() {
  const packs = useInvestigationPacks();
  const catalog = usePackBuilderCatalog();
  if (packs.isLoading || catalog.isLoading) return <p>loading</p>;
  return packs.data || catalog.data ? <p>found</p> : <p>Investigation packs unavailable</p>;
}

function CtemCasePage() {
  const kase = useCtemCase("unknown-id");
  if (kase.isLoading) return <p>loading</p>;
  return kase.data ? <p>found</p> : <p>Case not found</p>;
}

function render(page: React.ReactNode) {
  vi.spyOn(console, "error").mockImplementation(() => {});
  return renderWithProviders(
    <ErrorBoundary fallback={<p>page replaced</p>}>{page}</ErrorBoundary>,
    appQueryClient(),
  );
}

describe("detail pages on an unknown id", () => {
  it("a workspace 404 reaches the page as not found", async () => {
    get.mockRejectedValue(failure(404));
    render(<WorkspacePage />);
    expect(await screen.findByText("Workspace not found")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("an investigation pack 404 reaches the page as not found", async () => {
    get.mockRejectedValue(failure(404));
    render(<PackPage />);
    expect(await screen.findByText("Investigation pack not found")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("an investigation pack 403 (DEEP_INVESTIGATION off) reaches the page as not found", async () => {
    get.mockRejectedValue(failure(403));
    render(<PackPage />);
    expect(await screen.findByText("Investigation pack not found")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("the pack list and builder catalog 403 reach the page, not the error boundary", async () => {
    get.mockRejectedValue(failure(403));
    render(<PackListPage />);
    expect(await screen.findByText("Investigation packs unavailable")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("an investigation pack server error still reaches the error boundary", async () => {
    get.mockRejectedValue(failure(500));
    render(<PackPage />);
    expect(await screen.findByText("page replaced")).toBeTruthy();
  });

  it("both investigation pack routes sit behind the DEEP_INVESTIGATION gate", () => {
    const app = readFileSync(join(process.cwd(), "src/App.tsx"), "utf8");
    expect(app).toContain('path="/investigation-packs" element={<FeatureFlagGate flag="DEEP_INVESTIGATION">');
    expect(app).toContain('path="/investigation-packs/:id/:tab?" element={<FeatureFlagGate flag="DEEP_INVESTIGATION">');
  });

  it("a server error still reaches the error boundary", async () => {
    get.mockRejectedValue(failure(500));
    render(<WorkspacePage />);
    expect(await screen.findByText("page replaced")).toBeTruthy();
  });

  it("a CTEM case 404 (unknown or invisible to the caller) reaches the page as not found", async () => {
    get.mockRejectedValue(failure(404));
    render(<CtemCasePage />);
    expect(await screen.findByText("Case not found")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
  });

  it("a CTEM case server error still reaches the error boundary", async () => {
    get.mockRejectedValue(failure(500));
    render(<CtemCasePage />);
    expect(await screen.findByText("page replaced")).toBeTruthy();
  });

  it("an unknown CTEM stage reads no assessment before NotFoundState", () => {
    const page = readFileSync(join(process.cwd(), "src/pages/xtm-one/CtemStageDetail.tsx"), "utf8");
    expect(page).toContain("useCtemStageAssessment(validStage ? stage : \"\", vertical)");
    expect(page.indexOf("if (!validStage)")).toBeLessThan(page.indexOf("if (isLoading && !posture)"));
  });
});
