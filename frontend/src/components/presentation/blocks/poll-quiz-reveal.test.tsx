/**
 * A quiz answer revealed on a condition (xtm-one #10411): the bars fill live as
 * votes arrive, but the correct option is marked, and the "answered right"
 * share shown, only once the question's own `revealed` is true (the server sets
 * it after N votes, S seconds after the first vote, or never). A reset hides it
 * again. The static export (PPTX/PDF) renders through the same view.
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
  reveal: "after_votes",
  reveal_after_votes: 3,
  questions: [{ id: "q1", kind: "quiz", prompt: "2+2?", options: ["3", "4"], correct: 1 }],
} as DeckPollBlock;

function stateWith(counts: number[], revealed: boolean | undefined, pollRevealed = false): PollState {
  const total = counts.reduce((a, b) => a + b, 0);
  return {
    poll_id: "p1",
    code: "ABCDE",
    url: "https://example.test/v/ABCDE",
    qr: null,
    layout: "all",
    active_question: null,
    locked: false,
    revealed: pollRevealed,
    show_results: "live",
    voters: total,
    max_voters: 500,
    questions: [
      {
        id: "q1",
        kind: "quiz",
        prompt: "2+2?",
        options: ["3", "4"],
        max_words: 1,
        correct: 1,
        revealed,
        results: { id: "q1", kind: "quiz", counts, total },
      },
    ],
  } as PollState;
}

function view(state: PollState, spec: DeckPollBlock = block) {
  return (
    <DeckPollsContext.Provider value={{ presentationId: "d", data: { can_control: false, polls: [state] } as never }}>
      <PollBlockView block={spec} />
    </DeckPollsContext.Provider>
  );
}

const marked = (container: HTMLElement) => container.querySelectorAll('[data-correct="true"]').length;

describe("poll block, quiz answer revealed on a condition", () => {
  it("marks nothing at 0 votes: the waiting line alone", () => {
    const { container, getByText, queryByText } = renderWithProviders(view(stateWith([0, 0], false)));
    expect(marked(container)).toBe(0);
    expect(getByText("Waiting for the first vote")).toBeTruthy();
    expect(queryByText(/answered right/)).toBeNull();
  });

  it("fills the bars under the threshold with no option marked and no share", () => {
    const { container, getAllByText, queryByText } = renderWithProviders(view(stateWith([1, 1], false)));
    expect(getAllByText("1 · 50%")).toHaveLength(2);
    expect(marked(container)).toBe(0);
    expect(queryByText(/answered right/)).toBeNull();
  });

  it("marks the right option and shows the share once revealed, and hides both again on a reset", () => {
    const { container, getByText, queryByText, rerender } = renderWithProviders(view(stateWith([1, 2], true)));
    expect(marked(container)).toBe(1);
    expect(getByText("67% answered right")).toBeTruthy();
    rerender(view(stateWith([0, 0], false)));
    expect(marked(container)).toBe(0);
    expect(queryByText(/answered right/)).toBeNull();
  });

  it("never marks a reveal never quiz, even after the presenter's reveal", () => {
    const never = { ...block, reveal: "never", reveal_after_votes: undefined } as DeckPollBlock;
    const { container, queryByText } = renderWithProviders(view(stateWith([0, 4], undefined, true), never));
    expect(marked(container)).toBe(0);
    expect(queryByText(/answered right/)).toBeNull();
  });

  it("follows the poll's reveal when an older server sends no per-question state", () => {
    const manual = { ...block, reveal: "manual", reveal_after_votes: undefined } as DeckPollBlock;
    const { container } = renderWithProviders(view(stateWith([0, 1], undefined, true), manual));
    expect(marked(container)).toBe(1);
  });
});
