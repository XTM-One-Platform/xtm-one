// The download control of a file card is the design system's icon button (a
// fixed square with the DS radius), never a hand-made `rounded-full p-2`
// button, which took the line height of the DS Icon span and drew an oval on
// every chat surface (#11697). The working-file chip reads the shared chip
// classes of inline-chip.ts (DS radius, no pill). Asserted on roles and on the
// source, never on rendered class names (src/no-class-name-assertions.test.ts).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { FileChip, WorkingFileChip } from "@/components/chat-panel/FileChips";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessageAttachment } from "@/types";

const FILE_ID = "cccccccc-0000-4000-8000-000000000001";
const DIR = join(process.cwd(), "src", "components", "chat-panel");

const attachment = (file_tag: "download_file" | "working_file"): ChatMessageAttachment =>
  ({
    filename: "valuation.pdf",
    type: "pdf",
    content_type: "application/pdf",
    size: 1024,
    file_id: FILE_ID,
    file_tag,
    download_url: `/api/v1/chat/files/${FILE_ID}/download`,
  }) as ChatMessageAttachment;

describe("file card download control", () => {
  it("is a named button drawn by the design-system IconButton", () => {
    renderWithProviders(<FileChip att={attachment("download_file")} />);
    expect(screen.getByRole("button", { name: "Download valuation.pdf" })).toBeInTheDocument();
    const source = readFileSync(join(DIR, "FileChips.tsx"), "utf8");
    expect(source).toMatch(/import \{[^}]*\bIconButton\b[^}]*\} from "@filigran\/design-system"/);
    expect(source).toContain("<IconButton");
  });

  it("keeps the working-file chip on the shared chip, never a pill", () => {
    renderWithProviders(<WorkingFileChip att={attachment("working_file")} />);
    expect(screen.getByRole("button", { name: "Download valuation.pdf" })).toBeInTheDocument();
    const shared = readFileSync(join(DIR, "inline-chip.ts"), "utf8");
    expect(shared).toContain("rounded-sm");
    expect(shared).not.toMatch(/\brounded-full\b/);
  });
});
