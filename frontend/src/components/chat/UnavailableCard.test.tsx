// Every chat card renders the shared UnavailableCard when its object is gone:
// same frame and icon for a deleted video, presentation, report,
// investigation or scheduled work item.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { UnavailableCard } from "./UnavailableCard";
import { VideoCard } from "@/components/video-studio/VideoCard";
import { PresentationCard } from "@/components/presentation/PresentationCard";
import { ReportCard } from "@/components/report/ReportCard";
import { InvestigationBlock } from "@/components/investigation/InvestigationBlock";
import { ScheduledWorkItemCard } from "@/components/work-items/ScheduledWorkItemCard";

const gone = { data: undefined, isLoading: false, isError: true };

vi.mock("@/hooks/api/useVideoStudioApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useVideoStudioApi")>()),
  useVideoProject: () => gone,
}));
vi.mock("@/hooks/api/usePresentationApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/usePresentationApi")>()),
  usePresentation: () => gone,
}));
vi.mock("@/hooks/api/useReportApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useReportApi")>()),
  useReport: () => gone,
}));
vi.mock("@/hooks/api/useInvestigationApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useInvestigationApi")>()),
  useInvestigation: () => ({ ...gone, stalled: false }),
}));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItem: () => gone,
}));

const ID = "11111111-2222-3333-4444-555555555555";

function renderCard(ui: React.ReactElement) {
  return renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("UnavailableCard", () => {
  it("renders the message as a status line", () => {
    renderCard(<UnavailableCard message="This thing is no longer available." />);
    const card = screen.getByRole("status");
    expect(card.getAttribute("data-testid")).toBe("chat-card.unavailable");
    expect(card.textContent).toBe("This thing is no longer available.");
  });
});

describe.each([
  ["video", <VideoCard key="v" videoId={ID} />, "This video is no longer available."],
  ["presentation", <PresentationCard key="p" presentationId={ID} />, "This presentation is no longer available."],
  ["report", <ReportCard key="r" reportId={ID} />, "This report is no longer available."],
  ["investigation", <InvestigationBlock key="i" investigationId={ID} />, "This investigation is no longer available."],
  ["scheduled work item", <ScheduledWorkItemCard key="w" workItemId={ID} />, "This scheduled work item is no longer available."],
])("the %s chat card, object gone", (_kind, ui, copy) => {
  it("renders the shared unavailable card", () => {
    renderCard(ui);
    const card = screen.getByTestId("chat-card.unavailable");
    expect(card.textContent).toBe(copy);
    expect(screen.getAllByTestId("chat-card.unavailable")).toHaveLength(1);
  });
});
