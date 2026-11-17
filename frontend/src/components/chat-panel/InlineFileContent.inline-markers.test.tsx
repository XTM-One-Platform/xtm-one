// A stored answer read outside the chat panel (a run's result): a table whose
// cells carry markers stays ONE table, each cell drawing the marker's compact
// chip, and a marker on its own line keeps its card below the prose (#11697).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { ChatMessageAttachment } from "@/types";

import { InlineFileContent } from "@/components/chat-panel/InlineFileContent";

vi.mock("@/hooks/useFeatureFlag", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useFeatureFlag")>()),
  useFeatureFlag: () => true,
}));
vi.mock("@/components/chat-panel/FileChips", () => ({
  FileChip: ({ att }: { att: ChatMessageAttachment }) => <span data-testid="file-card">{att.filename}</span>,
  WorkingFileChip: ({ att }: { att: ChatMessageAttachment }) => <span data-testid="file-chip">{att.filename}</span>,
}));
vi.mock("@/components/chat-panel/MarkerInlineChip", () => ({
  MarkerInlineChip: ({ kind, id, slideRef }: { kind: string; id: string; slideRef?: string | null }) => (
    <span data-testid="inline-chip">{`${kind}:${id}#${slideRef ?? ""}`}</span>
  ),
}));
vi.mock("@/components/report/ReportCard", () => ({
  ReportCard: ({ reportId }: { reportId: string }) => <span data-testid="report-card">{reportId}</span>,
}));

const ids = [1, 2, 3, 4, 5].map((n) => `0000000${n}-aaaa-bbbb-cccc-dddddddddddd`);
const DECK = "11111111-2222-3333-4444-555555555555";
const REPORT = "66666666-7777-8888-9999-000000000000";

const attachments: ChatMessageAttachment[] = ids.map((id, i) => ({
  file_id: id,
  filename: `valuation-${i + 1}.pdf`,
  type: "PDF",
  size: 1024,
  content_type: "application/pdf",
  file_tag: "download_file",
  download_url: `/api/v1/chat/files/${id}/download`,
}));

describe("InlineFileContent, markers inside a table", () => {
  it("renders one whole table with a chip per row, and no raw pipe text", () => {
    const rows = ids.map((id, i) => `| ${i + 1} Oct 2024 | value ${i + 1} | date | [[FILE:${id}]] |`).join("\n");
    const content = `Here they are.\n\n| Date | Value | Kind | File |\n| --- | --- | --- | --- |\n${rows}\n`;
    const { container } = renderWithProviders(
      <MemoryRouter>
        <InlineFileContent content={content} attachments={attachments} />
      </MemoryRouter>,
    );
    expect(container.querySelectorAll("table")).toHaveLength(1);
    expect(container.querySelectorAll("tbody tr")).toHaveLength(5);
    const chips = screen.getAllByTestId("file-chip");
    expect(chips).toHaveLength(5);
    for (const chip of chips) expect(chip.closest("td")).not.toBeNull();
    expect(screen.queryByTestId("file-card")).toBeNull();
    expect(container.textContent).not.toContain("| ");
  });

  it("draws a deck (with its slide) and a report in their cells as chips", () => {
    const content = `| Item | Link |\n| --- | --- |\n| Deck | [[PRESENTATION:${DECK}#/s3]] |\n| Report | [[REPORT:${REPORT}]] |\n`;
    const { container } = renderWithProviders(
      <MemoryRouter>
        <InlineFileContent content={content} attachments={[]} />
      </MemoryRouter>,
    );
    expect(container.querySelectorAll("table")).toHaveLength(1);
    expect(screen.getAllByTestId("inline-chip").map((el) => el.textContent)).toEqual([
      `PRESENTATION:${DECK}#s3`,
      `REPORT:${REPORT}#`,
    ]);
    expect(screen.queryByTestId("report-card")).toBeNull();
  });

  it("keeps a numbered list whole around its markers", () => {
    const content = `1. first [[FILE:${ids[0]}]]\n2. second [[FILE:${ids[1]}]]\n3. third\n`;
    const { container } = renderWithProviders(
      <MemoryRouter>
        <InlineFileContent content={content} attachments={attachments} />
      </MemoryRouter>,
    );
    expect(container.querySelectorAll("ol")).toHaveLength(1);
    expect(container.querySelectorAll("ol > li")).toHaveLength(3);
    expect(screen.getAllByTestId("file-chip")).toHaveLength(2);
  });

  it("keeps the full card for a marker on its own line", () => {
    const content = `The report:\n\n[[FILE:${ids[0]}]]\n\n[[REPORT:${REPORT}]]`;
    renderWithProviders(
      <MemoryRouter>
        <InlineFileContent content={content} attachments={attachments} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("file-card").textContent).toBe("valuation-1.pdf");
    expect(screen.getByTestId("report-card").textContent).toBe(REPORT);
    expect(screen.queryByTestId("file-chip")).toBeNull();
    expect(screen.queryByTestId("inline-chip")).toBeNull();
  });
});
