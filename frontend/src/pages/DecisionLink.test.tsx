// SPDX-License-Identifier: Apache-2.0

// The answer link page (/decide/:token): an option link answers once in one
// click; an answered decision shows who, which option, when and answers
// nothing; a link for someone else, an expired one and an invalid one say so.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? "AI is disabled on this platform." : null),
}));

import DecisionLink from "./DecisionLink";

const ASK = {
  question: "Ship the release tonight?",
  options: [
    { key: "A", label: "Ship now" },
    { key: "B", label: "Wait for Monday" },
  ],
};

function view(over: Record<string, unknown> = {}) {
  return {
    decision_id: "d-1",
    status: "open",
    option: "A",
    option_label: "Ship now",
    ask: ASK,
    deadline: null,
    source_link: "/work-items?item=w-1",
    decided: null,
    ...over,
  };
}

const DECIDED = {
  status: "answered",
  option: "A",
  label: "Ship now",
  text: null,
  answered_by: "Ada Lovelace",
  answered_at: "2026-11-22T01:00:00Z",
  surface: "teams_chat",
};

function draw() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/decide/tok"]}>
      <Routes>
        <Route path="/decide/:token" element={<DecisionLink />} />
      </Routes>
    </MemoryRouter>,
  );
}

const refusal = (status: number) => Object.assign(new Error("refused"), { status });

beforeEach(() => {
  ai.off = false;
  api.get.mockReset();
  api.post.mockReset();
});
afterEach(cleanup);

describe("DecisionLink", () => {
  it("answers an option link once and shows the recorded answer", async () => {
    api.get.mockResolvedValue(view());
    api.post.mockResolvedValue(view({ status: "answered", decided: DECIDED, answered_now: true }));
    draw();
    expect(await screen.findByText("Your answer is recorded.")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/decisions/links/tok/answer", {});
    expect(screen.getByText("Ship now")).toBeInTheDocument();
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByTestId("decision-link-source")).toHaveAttribute("href", "/work-items?item=w-1");
  });

  it("answers nothing and says why when AI is off", async () => {
    ai.off = true;
    api.get.mockResolvedValue(view());
    draw();
    expect(await screen.findByText("AI is disabled on this platform.")).toBeInTheDocument();
    expect(screen.queryByTestId("decision-link-state")).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("shows the decided state and answers nothing on a closed decision", async () => {
    api.get.mockResolvedValue(view({ status: "answered", decided: DECIDED }));
    draw();
    expect(await screen.findByText("This decision is already answered.")).toBeInTheDocument();
    expect(screen.getByText("Opening the link again changes nothing.")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("says when the second click found the decision already answered", async () => {
    api.get.mockResolvedValue(view());
    api.post.mockResolvedValue(view({ status: "answered", decided: DECIDED, answered_now: false }));
    draw();
    expect(await screen.findByText("This decision is already answered.")).toBeInTheDocument();
  });

  it("takes a free answer on the text link", async () => {
    api.get.mockResolvedValue(view({ option: "text", option_label: null }));
    api.post.mockResolvedValue(
      view({ option: "text", status: "answered", decided: { ...DECIDED, option: null, label: null, text: "Next week" }, answered_now: true }),
    );
    draw();
    const field = await screen.findByLabelText("Or answer in your own words");
    expect(api.post).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "Next week" } });
    fireEvent.click(screen.getByRole("button", { name: /Send answer/ }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/decisions/links/tok/answer", { option: undefined, text: "Next week" }),
    );
    expect(await screen.findByText("Next week")).toBeInTheDocument();
  });

  it("draws an open decision with the shared decision card: context as Markdown, deadline, recommended option", async () => {
    api.get.mockResolvedValue(
      view({
        option: "text",
        option_label: null,
        deadline: "2026-11-24T17:00:00Z",
        ask: {
          ...ASK,
          recommendation: "A",
          context: "Three windows are **free** this week. See the [change calendar](https://example.org/cal).",
        },
      }),
    );
    draw();
    const card = await screen.findByTestId("decision-link-card");
    expect(card).toBeInTheDocument();
    expect(screen.getByTestId("decision-link-card-question").textContent).toBe("Ship the release tonight?");
    expect(screen.getByTestId("decision-link-card-deadline")).toBeInTheDocument();
    expect(screen.getByTestId("decision-link-card-recommended")).toBeInTheDocument();
    expect(screen.getAllByTestId("decision-link-card-option")).toHaveLength(2);
    const context = screen.getByTestId("decision-link-card-context");
    expect(context.textContent).not.toContain("**");
    expect(screen.getByRole("link", { name: "change calendar" })).toHaveAttribute("href", "https://example.org/cal");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("answers an option picked on the text link's card", async () => {
    api.get.mockResolvedValue(view({ option: "text", option_label: null }));
    api.post.mockResolvedValue(view({ option: "text", status: "answered", decided: { ...DECIDED, option: "B", label: "Wait for Monday" }, answered_now: true }));
    draw();
    fireEvent.click(await screen.findByRole("button", { name: /Wait for Monday/ }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/decisions/links/tok/answer", { option: "B", text: undefined }),
    );
    expect(await screen.findByText("Your answer is recorded.")).toBeInTheDocument();
  });

  it("shows the option link's card disabled while the answer is recorded", async () => {
    api.get.mockResolvedValue(view());
    api.post.mockReturnValue(new Promise(() => {}));
    draw();
    expect(await screen.findByTestId("decision-link-card")).toBeInTheDocument();
    expect(screen.getByTestId("decision-link-state")).toHaveAttribute("data-state", "recording");
    for (const option of screen.getAllByTestId("decision-link-card-option")) expect(option).toBeDisabled();
    expect(screen.queryByLabelText("Or answer in your own words")).not.toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it.each([
    [403, "elsewhere", "This answer link was sent to someone else."],
    [410, "expired", "This answer link has expired."],
    [404, "invalid", "This answer link is not valid."],
  ])("refuses a %s link without answering", async (status, state, title) => {
    api.get.mockRejectedValue(refusal(status));
    draw();
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByTestId("decision-link-state")).toHaveAttribute("data-state", state);
    expect(api.post).not.toHaveBeenCalled();
  });
});
