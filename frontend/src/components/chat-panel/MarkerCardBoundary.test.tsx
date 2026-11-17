// A marker card that fails says so in its own place: the text of the
// message and every other card stay on screen.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

import { InlineFileContent } from "@/components/chat-panel/InlineFileContent";

vi.mock("@/hooks/useFeatureFlag", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useFeatureFlag")>()),
  useFeatureFlag: () => true,
}));
vi.mock("@/components/work-items/ScheduledWorkItemCard", () => ({
  ScheduledWorkItemCard: () => {
    throw new Error("Work item not found");
  },
}));
vi.mock("@/components/report/ReportCard", () => ({
  ReportCard: () => <p>the report</p>,
}));

const WORK_ITEM = "11111111-2222-3333-4444-555555555555";
const REPORT = "66666666-7777-8888-9999-000000000000";

describe("a marker card that fails", () => {
  it("is replaced by a notice while the message and the other cards stay", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderWithProviders(
      <MemoryRouter>
        <InlineFileContent
          content={`Scheduled it.\n\n[[WORK_ITEM:${WORK_ITEM}]]\n[[REPORT:${REPORT}]]`}
          attachments={[]}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("This item could not be loaded.")).toBeTruthy();
    expect(screen.getByText("Scheduled it.")).toBeTruthy();
    expect(screen.getByText("the report")).toBeTruthy();
  });
});
