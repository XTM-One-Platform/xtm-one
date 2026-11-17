// The Chat page's own conversation list is a side read: when it fails,
// the page stays (welcome screen, composer) and the sidebar says so in one
// line. Run against the app's client, where every failed query is thrown to
// the nearest ErrorBoundary unless the hook says otherwise.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { useConversations } from "./useChatApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
}

function serverError(): Error {
  return Object.assign(new Error("Internal Server Error: raw backend detail"), { status: 500 });
}

/** The parts of the Chat page that must outlive a failed list read. */
function ChatShell({ tolerant }: { tolerant: boolean }) {
  const { isError } = useConversations(50, tolerant ? { throwOnError: false } : {});
  return (
    <div>
      <aside>{isError ? <p role="alert">Could not load your conversations.</p> : null}</aside>
      <h1>How can I help you?</h1>
      <textarea aria-label="Message" />
    </div>
  );
}

function renderShell(tolerant: boolean) {
  vi.spyOn(console, "error").mockImplementation(() => {});
  return renderWithProviders(
    <ErrorBoundary fallback={<p>page replaced</p>}>
      <ChatShell tolerant={tolerant} />
    </ErrorBoundary>,
    appQueryClient(),
  );
}

describe("the Chat page when its conversation list read fails", () => {
  it("keeps the welcome heading and the composer, and the sidebar shows the inline line", async () => {
    get.mockRejectedValue(serverError());
    renderShell(true);

    // The hook retries once (about 1 s) before it reports the failure.
    expect(await screen.findByRole("alert", undefined, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText("How can I help you?")).toBeTruthy();
    expect(screen.getByLabelText("Message")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
    expect(screen.queryByText(/raw backend detail/)).toBeNull();
  });

  it("still throws to the boundary for a caller that keeps the app policy", async () => {
    get.mockRejectedValue(serverError());
    renderShell(false);

    expect(await screen.findByText("page replaced", undefined, { timeout: 4000 })).toBeTruthy();
  });

  it("is read tolerant by the Chat page itself", () => {
    // The panel draws the list itself; the page reads it only to pick the conversation "Chat" resumes.
    const chat = readFileSync(join(process.cwd(), "src/pages/ChatPanelPage.tsx"), "utf8");
    expect(chat).toContain("useConversations(50, { throwOnError: false })");
  });
});
