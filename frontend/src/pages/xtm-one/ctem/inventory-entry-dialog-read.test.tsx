// SPDX-License-Identifier: Apache-2.0

// The Inventory entry dialog keeps a failed read of the row it updates in the dialog (UX round 1 on #1551, B1): a
// record removed since the list loaded (404) says so and refreshes the list, any other failure offers Retry, and the
// section error boundary never replaces the list. Run against the app's client, where every failed query is thrown
// to the nearest ErrorBoundary unless the hook says otherwise.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { InventoryEntryDialog } from "./InventoryEntryDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

function failure(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

function render(section: "assets_exposure" | "asset_groups", id: string) {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  renderWithProviders(
    <ErrorBoundary fallback={<p>section replaced</p>}>
      <p>the list</p>
      <InventoryEntryDialog section={section} mode={{ kind: "update", id }} onOpenChange={() => {}} />
    </ErrorBoundary>,
    client,
  );
  return { invalidate };
}

describe("InventoryEntryDialog on a failed row read", () => {
  it("a record 404 says the entry no longer exists and refreshes the list, without the error boundary", async () => {
    get.mockImplementation(async (path: string) => {
      if (path.includes("/assets/records/")) throw failure(404);
      return { items: [] };
    });
    const { invalidate } = render("assets_exposure", "gone-id");
    expect(await screen.findByText("This entry no longer exists. The list is refreshed.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("the list")).toBeInTheDocument();
    expect(screen.queryByText("section replaced")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    // The dialog's own X and the footer's Close.
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(2);
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["organization-assets"] }));
  });

  it("a record 5xx offers Retry, which reads the record again", async () => {
    let calls = 0;
    get.mockImplementation(async (path: string) => {
      if (path.includes("/assets/records/")) {
        calls += 1;
        throw failure(500);
      }
      return { items: [] };
    });
    render("assets_exposure", "some-id");
    // The hook retries a 5xx twice (with backoff) before the dialog shows the failure.
    expect(await screen.findByText("This entry could not be loaded.", undefined, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText("section replaced")).not.toBeInTheDocument();
    const before = calls;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await vi.waitFor(() => expect(calls).toBeGreaterThan(before));
  }, 15000);

  it("a group no longer in the list says the entry no longer exists", async () => {
    get.mockResolvedValue({ items: [{ id: "other", name: "DMZ edge", row_version: 1, member_count: 2 }] });
    render("asset_groups", "gone-group");
    expect(await screen.findByText("This entry no longer exists. The list is refreshed.")).toBeInTheDocument();
    expect(screen.queryByText("section replaced")).not.toBeInTheDocument();
  });

  it("a failed group list read stays in the dialog", async () => {
    get.mockRejectedValue(failure(503));
    render("asset_groups", "some-group");
    // The hook retries a 5xx twice (with backoff) before the dialog shows the failure.
    expect(await screen.findByText("This entry could not be loaded.", undefined, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText("section replaced")).not.toBeInTheDocument();
  });

  it("draws its alert lines in the error tone that reads 4.5:1, never error-primary", () => {
    // UX round 2 on #1551, B1: error-primary reads 4.26:1 on the dark dialog, error-tertiary 5.69:1 (dark) and
    // 8.98:1 (light). The token is pinned in the source for the read-failure line and the inline refusal line.
    const source = readFileSync(resolve(process.cwd(), "src/pages/xtm-one/ctem/InventoryEntryDialog.tsx"), "utf8");
    expect(source.match(/text-feedback-error-tertiary/g)?.length ?? 0).toBeGreaterThanOrEqual(2);
    expect(source).not.toMatch(/(^|[\s:"])text-feedback-error-primary/);
  });
});
