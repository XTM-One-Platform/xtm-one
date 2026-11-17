/**
 * The poll controls row under the slide (xtm-one #10315 change 6, UX round 1 B1):
 * centred under the deck like the fullscreen bar, and every control drawn as the
 * same outlined button ("Reset votes" was a ghost button that read as a link).
 */
import type { ButtonHTMLAttributes } from "react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckPollBlock, PollState } from "@/types";

import { PollControls } from "./PollControls";

let pollsData: { can_control: boolean; polls: PollState[] } | undefined;
vi.mock("@/hooks/api/usePresentationApi", () => ({
  useDeckPolls: () => ({ data: pollsData }),
  useControlDeckPoll: () => ({ mutate: vi.fn() }),
}));
// The variant a control asks for, read back through a stub of the app button.
vi.mock("@/components/ui/button", () => ({
  Button: ({ variant, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string }) => (
    <button type="button" data-variant={variant ?? "default"} {...props} />
  ),
}));

const block = {
  type: "poll",
  id: "poll-all",
  layout: "all",
  questions: [{ id: "q2", kind: "quiz", prompt: "2+2?", options: ["3", "4"], correct: 1 }],
} as DeckPollBlock;

const state = {
  poll_id: "poll-all",
  code: "ABCDE",
  url: "https://example.test/v/ABCDE",
  qr: null,
  layout: "all",
  active_question: null,
  locked: false,
  revealed: false,
  show_results: "live",
  voters: 0,
  max_voters: 500,
  questions: [],
} as PollState;

describe("poll controls row", () => {
  it("is centred under the slide", () => {
    pollsData = { can_control: true, polls: [state] };
    const { getByRole } = renderWithProviders(<PollControls presentationId="d" slide={{ blocks: [block] }} />);
    expect(getByRole("toolbar", { name: "Poll controls" }).getAttribute("data-align")).toBe("center");
  });

  it("draws reveal, lock and reset as the same outlined button", () => {
    pollsData = { can_control: true, polls: [state] };
    const { getByRole } = renderWithProviders(<PollControls presentationId="d" slide={{ blocks: [block] }} />);
    const variants = ["Reveal the answer", "Lock voting", "Reset votes"].map((name) =>
      getByRole("button", { name }).getAttribute("data-variant"),
    );
    expect(variants).toEqual(["outline", "outline", "outline"]);
  });

  // UX round 1 B1 of #10417: once the condition marks the answer, Reveal and Hide would change nothing.
  const conditional = { ...block, reveal: "after_votes", reveal_after_votes: 3 } as DeckPollBlock;
  const quizAt = (votes: number, revealedFlag = false): PollState => ({
    ...state,
    revealed: revealedFlag,
    questions: [
      {
        id: "q2",
        kind: "quiz",
        prompt: "2+2?",
        options: ["3", "4"],
        max_words: 1,
        correct: 1,
        revealed: revealedFlag || votes >= 3,
        condition_met: votes >= 3,
      },
    ],
  });

  it("offers no Reveal and no Hide once after_votes 3 holds 3 votes", () => {
    for (const flag of [false, true]) {
      pollsData = { can_control: true, polls: [quizAt(3, flag)] };
      const { queryByRole, getByRole, unmount } = renderWithProviders(
        <PollControls presentationId="d" slide={{ blocks: [conditional] }} />,
      );
      expect(queryByRole("button", { name: "Reveal the answer" })).toBeNull();
      expect(queryByRole("button", { name: "Hide the answer" })).toBeNull();
      expect(getByRole("button", { name: "Lock voting" })).toBeTruthy();
      unmount();
    }
  });

  it("keeps Reveal as the early reveal before the condition holds", () => {
    pollsData = { can_control: true, polls: [quizAt(2)] };
    const { getByRole } = renderWithProviders(<PollControls presentationId="d" slide={{ blocks: [conditional] }} />);
    expect(getByRole("button", { name: "Reveal the answer" })).toBeTruthy();
  });
});
