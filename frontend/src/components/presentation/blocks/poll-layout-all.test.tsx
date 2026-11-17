/**
 * The `poll` block in `layout: "all"` (xtm-one #10157, UX round 3 B1): every
 * question cell must be a plain element, never a `<section>`. reveal.js forces
 * `display: block !important` on each section under `.slides`, which voids the
 * cell's flex column: no gap between prompt and results, bars that do not
 * spread down the cell.
 */
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckPollBlock, PollState } from "@/types";

import { DeckPollsContext } from "../poll-context";
import { PollControls } from "../PollControls";
import { PollBlockView } from "./poll";

const controlPoll = vi.fn();
let pollsData: { can_control: boolean; polls: PollState[] } | undefined;
vi.mock("@/hooks/api/usePresentationApi", () => ({
  useDeckPolls: () => ({ data: pollsData }),
  useControlDeckPoll: () => ({ mutate: controlPoll }),
}));

const block: DeckPollBlock = {
  type: "poll",
  id: "poll-all",
  layout: "all",
  questions: [
    { id: "q1", kind: "choice", prompt: "Favourite colour", options: ["Red", "Blue"] },
    { id: "q2", kind: "quiz", prompt: "2+2?", options: ["3", "4", "5"], correct: 1 },
    { id: "q3", kind: "word_cloud", prompt: "One word" },
  ],
} as DeckPollBlock;

describe("poll block, layout all", () => {
  it("draws one non-section cell per question", () => {
    const { container } = renderWithProviders(<PollBlockView block={block} />);
    const cells = container.querySelectorAll(".deck-poll-cell");
    expect(cells).toHaveLength(3);
    for (const cell of cells) expect(cell.tagName).not.toBe("SECTION");
    expect(container.querySelector(".deck-poll section")).toBeNull();
  });

  const state: PollState = {
    poll_id: "poll-all",
    code: "ABCDE",
    url: "https://example.test/v/ABCDE",
    qr: null,
    layout: "all",
    active_question: null,
    locked: false,
    revealed: false,
    show_results: "live",
    voters: 2,
    max_voters: 500,
    questions: [],
  } as PollState;

  it("marks no question as current and titles every option label (xtm-one #10235)", () => {
    const { container } = renderWithProviders(
      <DeckPollsContext.Provider value={{ presentationId: "d", data: { can_control: true, polls: [state] } as never }}>
        <PollBlockView block={block} />
      </DeckPollsContext.Provider>,
    );
    expect(container.querySelector(".deck-poll-cell[data-open]")).toBeNull();
    const labels = Array.from(container.querySelectorAll(".deck-poll-bar-label")).map((el) => el.getAttribute("title"));
    expect(labels).toEqual(["Red", "Blue", "3", "4", "5"]);
  });

  it("draws no counter and no stepping for an all poll; lock and reset stay", () => {
    pollsData = { can_control: true, polls: [state] };
    const { queryByText, getByText } = renderWithProviders(<PollControls presentationId="d" slide={{ blocks: [block] }} />);
    expect(queryByText("Previous question")).toBeNull();
    expect(queryByText("Next question")).toBeNull();
    expect(queryByText(/Question \d+ of \d+/)).toBeNull();
    expect(getByText("Lock voting")).toBeTruthy();
    expect(getByText("Reset votes")).toBeTruthy();
  });

  it("marks the quiz answer from the start and offers no reveal with reveal live (#10315)", () => {
    const live = { ...block, reveal: "live", controls: "none" } as DeckPollBlock;
    pollsData = { can_control: true, polls: [state] };
    const { container, queryByText } = renderWithProviders(
      <DeckPollsContext.Provider value={{ presentationId: "d", data: { can_control: true, polls: [state] } as never }}>
        <PollBlockView block={live} />
        <PollControls presentationId="d" slide={{ blocks: [live] }} />
      </DeckPollsContext.Provider>,
    );
    const marked = Array.from(container.querySelectorAll('.deck-poll-bar[data-correct="true"] .deck-poll-bar-label'));
    expect(marked.map((el) => el.getAttribute("title"))).toEqual(["4"]);
    expect(queryByText("Reveal the answer")).toBeNull();
  });

  it("keeps the stepping controls of an active poll", () => {
    const active = { ...block, layout: "active" } as DeckPollBlock;
    pollsData = { can_control: true, polls: [{ ...state, layout: "active", active_question: "q2" }] };
    const { getByText } = renderWithProviders(<PollControls presentationId="d" slide={{ blocks: [active] }} />);
    expect(getByText("Question 2 of 3")).toBeTruthy();
    expect(getByText("Previous question")).toBeTruthy();
    expect(getByText("Next question")).toBeTruthy();
  });
});
