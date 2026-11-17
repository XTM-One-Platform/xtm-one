// "Go to the newer version" on a superseded file chip (#10910, parity with the
// pre-#6641 transcript): shown on an older revision only, it brings the newest
// file of the same name and type into view and focuses it.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { WorkingFileChip } from "@/components/chat-panel/FileChips";
import { conversationRevisions } from "@/pages/chat/file-revisions";
import { FileRevisionsContext } from "@/pages/chat/file-revisions-context";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessageAttachment } from "@/types";

const OLD_ID = "aaaaaaaa-0000-4000-8000-000000000001";
const NEW_ID = "aaaaaaaa-0000-4000-8000-000000000002";

const attachment = (file_id: string): ChatMessageAttachment =>
  ({
    filename: "report.md",
    type: "md",
    content_type: "text/markdown",
    file_id,
    file_tag: "working_file",
    download_url: `/api/v1/files/${file_id}/download`,
  }) as ChatMessageAttachment;

const revisions = conversationRevisions([
  { created_at: "2026-11-10T09:00:00Z", attachments: [attachment(OLD_ID)] },
  { created_at: "2026-11-10T10:00:00Z", attachments: [attachment(NEW_ID)] },
]);

function renderBoth() {
  return renderWithProviders(
    <FileRevisionsContext.Provider value={revisions}>
      <WorkingFileChip att={attachment(OLD_ID)} />
      <WorkingFileChip att={attachment(NEW_ID)} />
    </FileRevisionsContext.Provider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("NewerVersionLink", () => {
  it("shows on the superseded revision only", () => {
    renderBoth();
    expect(screen.getAllByRole("button", { name: "Go to the newer version" })).toHaveLength(1);
  });

  it("is absent outside a transcript and on a lone file", () => {
    renderWithProviders(<WorkingFileChip att={attachment(OLD_ID)} />);
    expect(screen.queryByRole("button", { name: "Go to the newer version" })).toBeNull();
  });

  it("brings the newer file into view and focuses it", () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
    renderBoth();
    fireEvent.click(screen.getByRole("button", { name: "Go to the newer version" }));
    const newer = document.querySelector<HTMLElement>(`[data-file-anchor="${NEW_ID}"]`);
    expect(newer).not.toBeNull();
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.contexts[0]).toBe(newer);
    expect(document.activeElement).toBe(newer);
  });
});
