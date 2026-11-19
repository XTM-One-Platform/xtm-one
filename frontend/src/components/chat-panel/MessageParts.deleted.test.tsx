// The transcript path of a deleted file (#396, UX r1 B1 on #405): the panel is
// never handed a tombstoned attachment (the restore leaves it out), so the host
// draws its deleted card under the message, never a download or an image fetch.
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { DeletedAttachments } from "@/components/chat-panel/MessageParts";
import { downloadErrorDescription } from "@/lib/download-error";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage, ChatMessageAttachment } from "@/types";

const DELETED_ID = "dddddddd-0000-4000-8000-000000000405";
const LIVE_ID = "eeeeeeee-0000-4000-8000-000000000405";

const attachment = (overrides: Partial<ChatMessageAttachment>): ChatMessageAttachment =>
  ({
    filename: "q3-notes.md",
    type: "md",
    content_type: "text/markdown",
    size: 2048,
    file_id: DELETED_ID,
    file_tag: "download_file",
    download_url: `/api/v1/chat/files/${DELETED_ID}/download`,
    ...overrides,
  }) as ChatMessageAttachment;

const message = (content: string, attachments: ChatMessageAttachment[]): ChatMessage =>
  ({ id: "m1", role: "user", content, attachments, created_at: "2026-11-19T10:00:00Z" }) as unknown as ChatMessage;

describe("deleted files of a transcript message", () => {
  it("draws the deleted card, not the live file, and fetches nothing", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderWithProviders(
      <DeletedAttachments
        stored={message("see attached", [
          attachment({ deleted_at: "2026-11-18T10:00:00Z", deleted_reason: "retention" }),
          attachment({ file_id: LIVE_ID, filename: "live.csv" }),
        ])}
      />,
    );
    const group = screen.getByTestId("message-deleted-files");
    expect(group).toHaveTextContent("q3-notes.md");
    expect(group).not.toHaveTextContent("live.csv");
    expect(screen.getByTestId("file-card-deleted")).toHaveTextContent(/by the retention policy/);
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("leaves a file the text names with a marker to the marker's card", () => {
    renderWithProviders(
      <DeletedAttachments
        stored={message(`here [[FILE:${DELETED_ID}]]`, [
          attachment({ deleted_at: "2026-11-18T10:00:00Z", deleted_reason: "retention" }),
        ])}
      />,
    );
    expect(screen.queryByTestId("message-deleted-files")).not.toBeInTheDocument();
  });

  it("names an administrator's deletion in the 410 download toast", () => {
    const t = (id: string, values?: Record<string, string | number>) =>
      id.replace("{filename}", String(values?.filename ?? ""));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(
      downloadErrorDescription({ status: 410, detail: { deleted_reason: "admin" } }, "q3-notes.md", t),
    ).toBe("\"q3-notes.md\" was deleted by an administrator.");
  });
});
