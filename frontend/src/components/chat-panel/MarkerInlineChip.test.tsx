// The compact chip a deck, report, video, dashboard, investigation or work
// item marker draws inside a table cell, a list item or a quote (#11697): the
// kind's icon, the title cut to fit, the kind, and a link to open it; a
// skeleton while it loads and "Unavailable" when it cannot be read.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { MarkerInlineChip } from "@/components/chat-panel/MarkerInlineChip";
import { renderWithProviders } from "@/testing/providers";

const ID = "11111111-2222-4333-8444-555555555555";
const render = (node: ReactElement) => renderWithProviders(<MemoryRouter>{node}</MemoryRouter>);
const state: { data: Record<string, unknown> | undefined; isLoading: boolean } = { data: undefined, isLoading: false };
const query = () => ({ data: state.data, isLoading: state.isLoading, isError: false });

vi.mock("@/hooks/api/usePresentationApi", () => ({ usePresentation: () => query() }));
vi.mock("@/hooks/api/useReportApi", () => ({ useReport: () => query() }));
vi.mock("@/hooks/api/useVideoStudioApi", () => ({ useVideoProject: () => query() }));
vi.mock("@/hooks/api/useCustomDashboardApi", () => ({ useCustomDashboard: () => query() }));
vi.mock("@/hooks/api/useInvestigationApi", () => ({ useInvestigation: () => query() }));
vi.mock("@/hooks/api/useWorkItemApi", () => ({ useWorkItem: () => query() }));

describe("MarkerInlineChip", () => {
  it.each([
    ["PRESENTATION", null, `/presentations/${ID}`, "Deck"],
    ["PRESENTATION", "s-3", `/presentations/${ID}#/s-3`, "Deck"],
    ["REPORT", null, `/reports/${ID}`, "Report"],
    ["VIDEO", null, `/videos/${ID}`, "Video"],
    ["DASHBOARD", null, `/dashboards/${ID}`, "Dashboard"],
    ["WORK_ITEM", null, `/tasks/work-items/${ID}`, "Work item"],
  ] as const)("links a %s (slide %s) to %s with its title and kind", (kind, slideRef, href, label) => {
    state.data = { id: ID, title: "Valuation 2024" };
    state.isLoading = false;
    render(<MarkerInlineChip kind={kind} id={ID} slideRef={slideRef} />);
    const chip = screen.getByTestId("marker-inline-chip");
    expect(chip.tagName).toBe("A");
    expect(chip.getAttribute("href")).toBe(href);
    expect(chip).toHaveTextContent("Valuation 2024");
    expect(chip).toHaveTextContent(label);
  });

  it("names an investigation by its subject, with no link of its own", () => {
    state.data = { id: ID, subject_value: "203.0.113.7" };
    state.isLoading = false;
    render(<MarkerInlineChip kind="INVESTIGATION" id={ID} />);
    const chip = screen.getByTestId("marker-inline-chip");
    expect(chip.tagName).toBe("SPAN");
    expect(chip).toHaveTextContent("203.0.113.7");
    expect(chip).toHaveTextContent("Investigation");
  });

  it("shows a skeleton while loading and says Unavailable when it cannot be read", () => {
    state.data = undefined;
    state.isLoading = true;
    const { unmount } = render(<MarkerInlineChip kind="REPORT" id={ID} />);
    expect(screen.getByTestId("marker-inline-chip")).toHaveAttribute("aria-busy", "true");
    unmount();
    state.isLoading = false;
    render(<MarkerInlineChip kind="REPORT" id={ID} />);
    expect(screen.getByTestId("marker-inline-chip")).toHaveTextContent("Unavailable");
  });
});
