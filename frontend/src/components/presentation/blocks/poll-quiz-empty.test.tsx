/**
 * A quiz question with no vote yet (a fresh poll, or a vote reset): only the
 * waiting line is drawn, never a "0% answered right" share under it. The share
 * comes with the first vote, right or wrong. The static export (PPTX/PDF)
 * renders through the same view, so the rule holds there too.
 */
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckPollBlock, PollState } from "@/types";

import { DeckPollsContext } from "../poll-context";
import { PollBlockView } from "./poll";

const block = {
  type: "poll",
  id: "p1",
  layout: "all",
  controls: "none",
  reveal: "live",
  questions: [
    { id: "q1", kind: "quiz", prompt: "2+2?", options: ["3", "4"], correct: 1 },
    { id: "q2", kind: "choice", prompt: "Colour?", options: ["Red", "Blue"] },
  ],
} as DeckPollBlock;

function stateWith(quizCounts: number[], choiceCounts: number[], layout: "all" | "active" = "all"): PollState {
  const sum = (counts: number[]) => counts.reduce((a, b) => a + b, 0);
  return {
    poll_id: "p1",
    code: "ABCDE",
    url: "https://example.test/v/ABCDE",
    qr: null,
    layout,
    active_question: layout === "active" ? "q1" : null,
    locked: false,
    revealed: false,
    show_results: "live",
    voters: Math.max(sum(quizCounts), sum(choiceCounts)),
    max_voters: 500,
    questions: [
      {
        id: "q1",
        kind: "quiz",
        prompt: "2+2?",
        options: ["3", "4"],
        max_words: 1,
        correct: 1,
        results: { id: "q1", kind: "quiz", counts: quizCounts, total: sum(quizCounts) },
      },
      {
        id: "q2",
        kind: "choice",
        prompt: "Colour?",
        options: ["Red", "Blue"],
        max_words: 1,
        correct: null,
        results: { id: "q2", kind: "choice", counts: choiceCounts, total: sum(choiceCounts) },
      },
    ],
  } as PollState;
}

function render(state: PollState, view: DeckPollBlock = block) {
  return renderWithProviders(
    <DeckPollsContext.Provider value={{ presentationId: "d", data: { can_control: false, polls: [state] } as never }}>
      <PollBlockView block={view} />
    </DeckPollsContext.Provider>,
  );
}

describe("poll block, quiz with no vote", () => {
  it("draws only the waiting line at 0 votes, with layout all", () => {
    const { container, queryByText } = render(stateWith([0, 0], [0, 0]));
    expect(container.querySelectorAll(".deck-poll-waiting")).toHaveLength(2);
    expect(container.querySelector(".deck-poll-score")).toBeNull();
    expect(queryByText(/answered right/)).toBeNull();
  });

  it("draws only the waiting line at 0 votes, with layout active", () => {
    const active = { ...block, layout: "active" } as DeckPollBlock;
    const { container } = render(stateWith([0, 0], [0, 0], "active"), active);
    expect(container.querySelectorAll(".deck-poll-waiting")).toHaveLength(1);
    expect(container.querySelector(".deck-poll-score")).toBeNull();
  });

  it("shows the share from the first vote, wrong or right, and drops the waiting line", () => {
    const wrong = render(stateWith([1, 0], [0, 0]));
    expect(wrong.container.querySelector(".deck-poll-score")?.textContent).toBe("0% answered right");
    // Only the choice question still waits.
    expect(wrong.container.querySelectorAll(".deck-poll-waiting")).toHaveLength(1);
    wrong.unmount();

    const right = render(stateWith([0, 1], [0, 0]));
    expect(right.container.querySelector(".deck-poll-score")?.textContent).toBe("100% answered right");
  });

  it("goes back to the waiting line alone after a vote reset, without a reload", () => {
    const voted = stateWith([0, 1], [1, 0]);
    const reset = stateWith([0, 0], [0, 0]);
    const { container, rerender } = render(voted);
    expect(container.querySelector(".deck-poll-score")).not.toBeNull();
    rerender(
      <DeckPollsContext.Provider value={{ presentationId: "d", data: { can_control: false, polls: [reset] } as never }}>
        <PollBlockView block={block} />
      </DeckPollsContext.Provider>,
    );
    expect(container.querySelector(".deck-poll-score")).toBeNull();
    expect(container.querySelectorAll(".deck-poll-waiting")).toHaveLength(2);
  });
});
