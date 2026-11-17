// A failed workspaces read degrades the chat sidebar to its flat layout and
// never replaces the Chat page with the error boundary. The sidebar reads the
// list with `throwOnError: false`; the "File in a workspace" dialog, mounted
// closed on every chat page, observes the same query, and the app policy
// (`throwOnError: true`, main.tsx) used to hand that cached error to the
// boundary even though the dialog never fetched.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { FileConversationDialog } from "@/components/chat/FileConversationDialog";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), patch: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: false }),
}));

const get = vi.mocked(api.get);

function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
}

function serverError(): Error {
  return Object.assign(new Error("Internal Server Error"), { status: 500 });
}

/** The parts of the Chat page a failed workspaces read must leave in place. */
function ChatShell({ dialogOpen }: { dialogOpen: boolean }) {
  // What `useSidebarWorkspaces` asks for: grouped only once the list answered.
  const { isSuccess, isError } = useWorkspaces({ throwOnError: false });
  return (
    <div>
      <aside data-sidebar-layout={isSuccess ? "grouped" : "flat"}>
        {isError ? <span data-testid="workspaces-failed" /> : null}
      </aside>
      <main data-testid="transcript">
        <h1>How can I help you?</h1>
        <textarea aria-label="Message" />
      </main>
      <FileConversationDialog
        conversationId="c-1"
        currentWorkspaceId={null}
        open={dialogOpen}
        onClose={() => {}}
      />
    </div>
  );
}

function renderShell(dialogOpen = false) {
  vi.spyOn(console, "error").mockImplementation(() => {});
  return renderWithProviders(
    <ErrorBoundary fallback={<p>page replaced</p>}>
      <ChatShell dialogOpen={dialogOpen} />
    </ErrorBoundary>,
    appQueryClient(),
  );
}

describe("the Chat page when GET /workspaces fails", () => {
  it("keeps the transcript mounted and the sidebar flat", async () => {
    get.mockRejectedValue(serverError());
    const { container } = renderShell();

    expect(await screen.findByTestId("workspaces-failed")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
    expect(screen.getByTestId("transcript")).toBeTruthy();
    expect(screen.getByLabelText("Message")).toBeTruthy();
    expect(container.querySelector("[data-sidebar-layout='flat']")).not.toBeNull();
  });

  it("says the failure inside the filing dialog, not with the page boundary", async () => {
    get.mockRejectedValue(serverError());
    renderShell(true);

    expect(await screen.findByText("The workspaces could not be loaded.")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
    expect(screen.getByTestId("transcript")).toBeTruthy();
  });
});
