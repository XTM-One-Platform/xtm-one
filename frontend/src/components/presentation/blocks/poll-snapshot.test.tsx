/**
 * The `poll` block as the PDF / PPTX export draws it (xtm-one #10157, slice S4):
 * the static render page provides the poll state it read from `render-polls`,
 * so the block shows the real short link, the voters and the results instead of
 * the `/v/…` placeholder and empty bars. The quiz answer is marked only once
 * revealed; without a provider (a public link) the placeholder stays.
 */
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckPollBlock, DeckPollsResponse } from "@/types";

import { DeckPollsContext } from "../poll-context";
import { PollBlockView } from "./poll";

const block: DeckPollBlock = {
  type: "poll",
  id: "poll-all",
  layout: "all",
  questions: [
    { id: "q1", kind: "quiz", prompt: "2+2?", options: ["3", "4", "5"], correct: 1 },
    { id: "q2", kind: "choice", prompt: "Favourite colour", options: ["Red", "Blue"] },
    { id: "q3", kind: "word_cloud", prompt: "One word" },
  ],
} as DeckPollBlock;

function snapshot(revealed: boolean, votes: boolean): DeckPollsResponse {
  return {
    can_control: false,
    polls: [
      {
        poll_id: "poll-all",
        code: "ABC123",
        url: "https://one.example.com/v/ABC123",
        qr: "data:image/svg+xml;base64,PHN2Zy8+",
        active_question: "q1",
        locked: false,
        revealed,
        show_results: "live",
        voters: votes ? 4 : 0,
        max_voters: 500,
        questions: [
          {
            id: "q1",
            kind: "quiz",
            prompt: "2+2?",
            options: ["3", "4", "5"],
            max_words: 1,
            correct: 1,
            results: { id: "q1", kind: "quiz", total: votes ? 4 : 0, counts: votes ? [1, 3, 0] : [0, 0, 0] },
          },
          {
            id: "q2",
            kind: "choice",
            prompt: "Favourite colour",
            options: ["Red", "Blue"],
            max_words: 1,
            results: { id: "q2", kind: "choice", total: votes ? 2 : 0, counts: votes ? [2, 0] : [0, 0] },
          },
          {
            id: "q3",
            kind: "word_cloud",
            prompt: "One word",
            options: [],
            max_words: 1,
            results: { id: "q3", kind: "word_cloud", total: votes ? 2 : 0, words: votes ? [{ text: "growth", count: 2 }] : [] },
          },
        ],
      },
    ],
  } as DeckPollsResponse;
}

function renderSnapshot(data: DeckPollsResponse | null) {
  return renderWithProviders(
    <DeckPollsContext.Provider value={data ? { presentationId: "deck", data } : null}>
      <PollBlockView block={block} />
    </DeckPollsContext.Provider>,
  );
}

describe("poll block in the export snapshot", () => {
  it("draws the real short link, the QR, the voters and the results", () => {
    const { container } = renderSnapshot(snapshot(false, true));
    expect(container.querySelector(".deck-poll-link-host")?.textContent).toBe("one.example.com");
    expect(container.querySelector(".deck-poll-link-code")?.textContent).toBe("/v/ABC123");
    expect(container.querySelector("img.deck-poll-qr")).not.toBeNull();
    const values = Array.from(container.querySelectorAll(".deck-poll-bar-value")).map((el) => el.textContent);
    expect(values).toEqual(["1 · 25%", "3 · 75%", "0 · 0%", "2 · 100%", "0 · 0%"]);
    expect(container.querySelector(".deck-poll-word")?.textContent).toBe("growth");
    // Not revealed: the quiz answer is not marked.
    expect(container.querySelector('[data-correct="true"]')).toBeNull();
  });

  it("marks the quiz answer only once revealed", () => {
    const { container } = renderSnapshot(snapshot(true, true));
    expect(container.querySelectorAll('[data-correct="true"]')).toHaveLength(1);
  });

  it("draws the link and 0% bars when nobody voted yet", () => {
    const { container } = renderSnapshot(snapshot(false, false));
    expect(container.querySelector(".deck-poll-link-code")?.textContent).toBe("/v/ABC123");
    const values = Array.from(container.querySelectorAll(".deck-poll-bar-value")).map((el) => el.textContent);
    expect(values.every((value) => value?.endsWith("0%"))).toBe(true);
  });

  it("keeps the placeholder without a snapshot (a public link)", () => {
    const { container } = renderSnapshot(null);
    expect(container.querySelector(".deck-poll-link-code")?.textContent).toBe("/v/…");
    expect(container.querySelector("img.deck-poll-qr")).toBeNull();
  });
});
