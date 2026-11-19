// A file whose content the retention policy (or its owner, or an
// administrator) deleted keeps its card (#396): the name, the size and when
// and why it went, and no download, no thumbnail fetch, no broken image.
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { FileChip, WorkingFileChip, WorkingFilesDisclosure } from "@/components/chat-panel/FileChips";
import { downloadErrorDescription } from "@/lib/download-error";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessageAttachment } from "@/types";

const FILE_ID = "dddddddd-0000-4000-8000-000000000396";

const deleted = (overrides: Partial<ChatMessageAttachment> = {}): ChatMessageAttachment =>
  ({
    filename: "q3-notes.md",
    type: "md",
    content_type: "text/markdown",
    size: 2048,
    file_id: FILE_ID,
    file_tag: "download_file",
    download_url: `/api/v1/chat/files/${FILE_ID}/download`,
    deleted_at: "2026-11-19T10:00:00Z",
    deleted_reason: "retention",
    ...overrides,
  }) as ChatMessageAttachment;

describe("deleted file card", () => {
  it("shows the name, the size and the retention notice, without a download", () => {
    renderWithProviders(<FileChip att={deleted()} />);
    const card = screen.getByTestId("file-card-deleted");
    expect(card).toHaveTextContent("q3-notes.md");
    expect(card).toHaveTextContent("2.0 KB");
    expect(card).toHaveTextContent(/Deleted on .*2026 by the retention policy/);
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("names an administrator's deletion", () => {
    renderWithProviders(<FileChip att={deleted({ deleted_reason: "admin" })} />);
    expect(screen.getByTestId("file-card-deleted")).toHaveTextContent(/by an administrator/);
  });

  it("keeps the working-file chip compact and not a button", () => {
    renderWithProviders(<WorkingFileChip att={deleted({ file_tag: "working_file" })} />);
    const chip = screen.getByTestId("file-chip-deleted");
    expect(chip).toHaveTextContent("q3-notes.md");
    expect(chip.tagName).not.toBe("BUTTON");
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("never fetches the thumbnail of a deleted picture", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderWithProviders(
      <WorkingFilesDisclosure
        files={[deleted({ filename: "frame.png", type: "png", content_type: "image/png", file_tag: "working_file" })]}
      />,
    );
    screen.getByRole("button", { name: /Working files/ }).click();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("explains a 410 Gone download in the toast", () => {
    const t = (id: string, values?: Record<string, string | number>) =>
      id.replace("{filename}", String(values?.filename ?? ""));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(downloadErrorDescription({ status: 410 }, "q3-notes.md", t)).toBe(
      "\"q3-notes.md\" was deleted by the retention policy.",
    );
  });
});
