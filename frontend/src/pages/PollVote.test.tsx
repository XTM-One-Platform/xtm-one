/**
 * The audience page `/v/CODE` (xtm-one #10235): a poll with `layout: "all"`
 * lists every question on one page, each answered on its own and in any
 * order; a stepped poll (`layout: "active"`) shows the open question alone.
 */
import { fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { PollState } from "@/types";

import PollVote from "./PollVote";

const question = (id: string, prompt: string) => ({
  id,
  kind: "choice" as const,
  prompt,
  options: ["Yes", "No"],
  max_words: 1,
});

function state(layout: "all" | "active"): PollState {
  const all = [question("q1", "First question"), question("q2", "Second question"), question("q3", "Third question")];
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
    questions: layout === "all" ? all : [all[1]],
  } as PollState;
}

let votes: { question_id: string; answer: unknown }[];

function mockServer(layout: "all" | "active") {
  votes = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        votes.push(JSON.parse(String(init.body)));
        return new Response("{}", { status: 200 });
      }
      return new Response(JSON.stringify(state(layout)), { status: 200 });
    }),
  );
}

function renderPage() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/v/ABCDE"]}>
      <Routes>
        <Route path="/v/:code" element={<PollVote />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("PollVote", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("lists every question of an all poll and votes on two of them in any order", async () => {
    mockServer("all");
    const page = renderPage();
    await page.findByText("Third question");
    expect(page.getByText("First question")).toBeTruthy();
    expect(page.getByText("Second question")).toBeTruthy();

    const sections = () => Array.from(page.container.querySelectorAll("section[data-question]")) as HTMLElement[];
    expect(sections()).toHaveLength(3);

    const voteOn = async (id: string, option: string) => {
      const section = sections().find((s) => s.dataset.question === id)!;
      const pick = Array.from(section.querySelectorAll("button[role=radio]")).find((b) => b.textContent === option)!;
      fireEvent.click(pick);
      const submit = Array.from(section.querySelectorAll("button")).find((b) => b.textContent === "Vote")!;
      fireEvent.click(submit);
    };
    await voteOn("q3", "No");
    await waitFor(() => expect(votes).toHaveLength(1));
    await voteOn("q1", "Yes");
    await waitFor(() => expect(votes).toHaveLength(2));
    expect(votes).toEqual([
      { question_id: "q3", voter: expect.any(String), answer: 1 },
      { question_id: "q1", voter: expect.any(String), answer: 0 },
    ]);
    // The untouched question still offers its vote; the two answered ones thank the voter.
    await waitFor(() => expect(page.getAllByText("Thanks, your vote is in. Look at the screen.")).toHaveLength(2));
    const second = sections().find((s) => s.dataset.question === "q2")!;
    expect(Array.from(second.querySelectorAll("button")).some((b) => b.textContent === "Vote")).toBe(true);
  });

  it("shows the open question alone on a stepped poll", async () => {
    mockServer("active");
    const page = renderPage();
    await page.findByText("Second question");
    expect(page.queryByText("First question")).toBeNull();
    expect(page.container.querySelectorAll("section[data-question]")).toHaveLength(1);
  });
});
