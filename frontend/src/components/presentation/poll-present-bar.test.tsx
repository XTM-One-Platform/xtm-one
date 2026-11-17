/**
 * The poll controls of a deck presented fullscreen on ONE screen (xtm-one
 * #10235): the bottom bar, the L / V / Shift+R / N / P keys and the toast that
 * confirms each action, for a guided (layout active) and an autonomous
 * (layout all) poll.
 */
import { act, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckPollBlock, PollState } from "@/types";

import { PollPresentBar } from "./PollControls";

const mutate = vi.fn();
let pollsData: { can_control: boolean; polls: PollState[] } | undefined;
vi.mock("@/hooks/api/usePresentationApi", () => ({
  useDeckPolls: () => ({ data: pollsData }),
  useControlDeckPoll: () => ({ mutate }),
}));

const questions = [
  { id: "q1", kind: "choice", prompt: "Colour", options: ["Red", "Blue"] },
  { id: "q2", kind: "quiz", prompt: "2+2?", options: ["3", "4"], correct: 1 },
  { id: "q3", kind: "word_cloud", prompt: "One word" },
];

function state(layout: "active" | "all", extra: Partial<PollState> = {}): PollState {
  return {
    poll_id: "p",
    code: "ABCDE",
    url: "https://example.test/v/ABCDE",
    qr: null,
    layout,
    active_question: layout === "all" ? null : "q2",
    locked: false,
    revealed: false,
    show_results: "live",
    voters: 0,
    max_voters: 500,
    questions: [],
    ...extra,
  } as PollState;
}

function slide(layout: "active" | "all", extra: Partial<DeckPollBlock> = {}) {
  return { blocks: [{ type: "poll", id: "p", layout, questions, ...extra } as unknown as DeckPollBlock] };
}

function press(key: string, init: KeyboardEventInit = {}) {
  act(() => {
    fireEvent.keyDown(document.body, { key, ...init });
  });
}

const sent = () => mutate.mock.calls.map(([call]) => [call.action, call.questionId]);

describe("PollPresentBar", () => {
  beforeEach(() => {
    mutate.mockReset();
  });

  it("draws nothing for a reader who cannot control the poll", () => {
    pollsData = { can_control: false, polls: [state("all")] };
    const { queryByTestId } = renderWithProviders(<PollPresentBar presentationId="d" slide={slide("all")} stage={null} />);
    expect(queryByTestId("presentation.poll-present-bar")).toBeNull();
    press("l");
    expect(mutate).not.toHaveBeenCalled();
  });

  it("drives an autonomous poll with L and V, ignores N and P, and confirms each action", () => {
    pollsData = { can_control: true, polls: [state("all")] };
    const { getByTestId, queryByText } = renderWithProviders(
      <PollPresentBar presentationId="d" slide={slide("all")} stage={null} />,
    );
    expect(getByTestId("presentation.poll-present-bar")).toBeTruthy();
    expect(queryByText("Next question")).toBeNull();
    press("l");
    expect(getByTestId("presentation.poll-toast").textContent).toBe("Voting locked");
    press("v");
    press("n");
    press("p");
    expect(sent()).toEqual([
      ["lock", undefined],
      ["reveal", undefined],
    ]);
  });

  it("steps a guided poll with N and P", () => {
    pollsData = { can_control: true, polls: [state("active")] };
    renderWithProviders(<PollPresentBar presentationId="d" slide={slide("active")} stage={null} />);
    press("n");
    press("p");
    expect(sent()).toEqual([
      ["next", undefined],
      ["open", "q1"],
    ]);
  });

  it("asks before Shift+R resets: Esc cancels, Enter confirms", () => {
    pollsData = { can_control: true, polls: [state("all", { locked: true })] };
    const { getByText, queryByText } = renderWithProviders(
      <PollPresentBar presentationId="d" slide={slide("all")} stage={null} />,
    );
    press("R", { shiftKey: true });
    expect(getByText("Delete all votes? Enter to confirm, Esc to cancel")).toBeTruthy();
    press("Escape");
    expect(queryByText("Delete all votes? Enter to confirm, Esc to cancel")).toBeNull();
    expect(mutate).not.toHaveBeenCalled();
    press("R", { shiftKey: true });
    press("Enter");
    press("l");
    expect(sent()).toEqual([
      ["reset", undefined],
      ["unlock", undefined],
    ]);
  });

  it("draws nothing and takes no key on a hands-free poll (controls none, #10315)", () => {
    pollsData = { can_control: true, polls: [state("all")] };
    const { queryByTestId } = renderWithProviders(
      <PollPresentBar presentationId="d" slide={slide("all", { controls: "none" })} stage={null} />,
    );
    expect(queryByTestId("presentation.poll-present-bar")).toBeNull();
    press("l");
    press("v");
    press("R", { shiftKey: true });
    press("Enter");
    expect(mutate).not.toHaveBeenCalled();
    expect(queryByTestId("presentation.poll-toast")).toBeNull();
  });

  it("leaves the arrows and modified keys to the deck", () => {
    pollsData = { can_control: true, polls: [state("active")] };
    renderWithProviders(<PollPresentBar presentationId="d" slide={slide("active")} stage={null} />);
    press("ArrowRight");
    press("l", { ctrlKey: true });
    expect(mutate).not.toHaveBeenCalled();
  });
});
