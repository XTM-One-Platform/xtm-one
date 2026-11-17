// The ROI outcome review row of a run or a work item (#9365).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { RoiReviewBlock } from "@/types";
import { OutcomeReview } from "./outcome-review";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const put = vi.mocked(api.put);

function block(over: Partial<RoiReviewBlock> = {}): RoiReviewBlock {
  return {
    record_id: "rec-1",
    unit: "contract_reviewed",
    unit_name: "Contract reviewed",
    units: 1,
    outcome: null,
    review_min: null,
    default_review_min: 10,
    review_note: null,
    reviewed_at: null,
    can_review: true,
    ...over,
  };
}

const outcome = (key: string) => document.querySelector(`[data-outcome="${key}"]`) as HTMLButtonElement;

describe("OutcomeReview", () => {
  beforeEach(() => {
    put.mockReset();
    put.mockResolvedValue({});
  });
  afterEach(cleanup);

  it("renders nothing without a block or without the right to review", () => {
    const { container, rerender } = renderWithProviders(<OutcomeReview block={null} />);
    expect(container.querySelector("[data-testid='outcome-review']")).toBeNull();
    rerender(<OutcomeReview block={block({ can_review: false })} />);
    expect(container.querySelector("[data-testid='outcome-review']")).toBeNull();
  });

  it("shows an unreviewed record with no outcome pressed and says how it counts", () => {
    renderWithProviders(<OutcomeReview block={block()} />);
    for (const key of ["accepted", "modified", "rejected"]) {
      expect(outcome(key).getAttribute("aria-pressed")).toBe("false");
    }
    expect(screen.getByTestId("outcome-review-unreviewed")).toBeTruthy();
    expect(screen.getByRole("group", { name: "ROI outcome" })).toBeTruthy();
  });

  it("marks a record rejected in one click", async () => {
    renderWithProviders(<OutcomeReview block={block()} />);
    fireEvent.click(outcome("rejected"));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/roi/records/rec-1/outcome", { outcome: "rejected", note: null }));
  });

  it("returns the record to unreviewed when the pressed outcome is clicked again", async () => {
    renderWithProviders(<OutcomeReview block={block({ outcome: "accepted" })} />);
    expect(outcome("accepted").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(outcome("accepted"));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/roi/records/rec-1/outcome", { outcome: null }));
  });

  it("never saves Modified on the click: it opens the review time field first", () => {
    renderWithProviders(<OutcomeReview block={block()} />);
    fireEvent.click(outcome("modified"));
    expect(put).not.toHaveBeenCalled();
    expect(outcome("modified").getAttribute("aria-pressed")).toBe("true");
    const minutes = screen.getByLabelText("Review time") as HTMLInputElement;
    expect(minutes.value).toBe("");
    expect(screen.getByTestId("outcome-review-minutes-help").textContent).toContain("10");
    fireEvent.click(screen.getByTestId("outcome-review-save"));
    expect(screen.getByTestId("outcome-review-minutes-error")).toBeTruthy();
    expect(put).not.toHaveBeenCalled();
    // A second click on the unsaved Modified cancels it, still with no request.
    fireEvent.click(outcome("modified"));
    expect(put).not.toHaveBeenCalled();
    expect(screen.getByTestId("outcome-review-unreviewed")).toBeTruthy();
  });

  it("asks for the review time of a modified record before saving it", async () => {
    renderWithProviders(<OutcomeReview block={block({ outcome: "modified" })} />);
    const minutes = screen.getByLabelText("Review time") as HTMLInputElement;
    expect(minutes.placeholder).toBe("");
    fireEvent.click(screen.getByTestId("outcome-review-save"));
    expect(screen.getByTestId("outcome-review-minutes-error")).toBeTruthy();
    expect(minutes.getAttribute("aria-invalid")).toBe("true");
    expect(put).not.toHaveBeenCalled();

    fireEvent.change(minutes, { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: " fixed two clauses " } });
    fireEvent.click(screen.getByTestId("outcome-review-save"));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/roi/records/rec-1/outcome", { outcome: "modified", note: "fixed two clauses", review_min: 25 }),
    );
  });

  it("shows the review time error in place of the help line, then the help again once valid", () => {
    renderWithProviders(<OutcomeReview block={block({ outcome: "modified" })} />);
    const minutes = screen.getByLabelText("Review time") as HTMLInputElement;
    const helpId = screen.getByTestId("outcome-review-minutes-help").id;
    expect(minutes.getAttribute("aria-describedby")).toBe(helpId);

    fireEvent.change(minutes, { target: { value: "-2" } });
    fireEvent.click(screen.getByTestId("outcome-review-save"));
    const error = screen.getByTestId("outcome-review-minutes-error");
    expect(screen.queryByTestId("outcome-review-minutes-help")).toBeNull();
    expect(minutes.getAttribute("aria-describedby")).toBe(error.id);

    fireEvent.change(minutes, { target: { value: "5" } });
    expect(screen.queryByTestId("outcome-review-minutes-error")).toBeNull();
    expect(screen.getByTestId("outcome-review-minutes-help")).toBeTruthy();
    expect(minutes.getAttribute("aria-describedby")).toBe(helpId);
    expect(minutes.getAttribute("aria-invalid")).toBeNull();
  });

  it("shows one note counter and caps the note at 500 characters", () => {
    renderWithProviders(<OutcomeReview block={block({ outcome: "accepted" })} />);
    const note = screen.getByLabelText("Note (optional)") as HTMLInputElement;
    fireEvent.change(note, { target: { value: "x".repeat(520) } });
    expect(note.value.length).toBe(500);
    // One counter only: the design-system Input draws its own when given maxLength.
    expect(note.getAttribute("maxlength")).toBeNull();
    expect(screen.getAllByText(/500\s*\/\s*500|500\/500/)).toHaveLength(1);
    expect(screen.getByTestId("outcome-review-note-count").textContent).toBe("500 / 500");
    expect(note.getAttribute("aria-describedby")).toBe(screen.getByTestId("outcome-review-note-count").id);
  });
});
