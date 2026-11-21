// An external run that asked its caller a question: its A2A task waits on the
// calling agent, and the caller's answer starts another run on the same task.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { runStatusConfig } from "@/lib/constants";
import { renderWithProviders } from "@/testing/providers";
import type { A2AExchange } from "@/types";
import { A2ARunExchange } from "./A2ARunExchange";
import { a2aNoResultText } from "./a2a-no-result-text";

const t_i18n = (id: string) => id;

function renderExchange(exchange: A2AExchange | null) {
  return renderWithProviders(
    <MemoryRouter>
      <A2ARunExchange exchange={exchange} />
    </MemoryRouter>,
  );
}

const waiting: A2AExchange = {
  asked: { question: "Which currency is the budget in?", state: "waiting", answered_by_run_id: null },
};

describe("A2ARunExchange", () => {
  it("shows a run waiting on its caller with the question it asked", () => {
    renderExchange(waiting);

    expect(screen.getByText("Waiting on its caller")).toBeTruthy();
    expect(screen.getByText("Which currency is the budget in?")).toBeTruthy();
    expect(screen.getByText(/waiting for the agent that submitted it to answer/)).toBeTruthy();
    // Nobody here answers: the caller is the partner agent.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("links an answered question to the run the answer started", () => {
    renderExchange({ asked: { question: "Which currency?", state: "answered", answered_by_run_id: "run-2" } });

    expect(screen.getByText("Asked its caller")).toBeTruthy();
    expect(screen.getByText("Answered")).toBeTruthy();
    const link = screen.getByRole("link", { name: /View the run that continued it/ });
    expect(link.getAttribute("href")).toBe("/operations/executions/run-2");
  });

  it("says a question was withdrawn when the task was cancelled first", () => {
    renderExchange({ asked: { question: "Which currency?", state: "withdrawn", answered_by_run_id: null } });

    expect(screen.getByText("Cancelled before an answer")).toBeTruthy();
    expect(screen.queryByText("Waiting on its caller")).toBeNull();
  });

  it("shows what a continued run answers and links back to the run that asked", () => {
    renderExchange({ answers: { question: "Which currency?", answer: "NZD, 9000", previous_run_id: "run-1" } });

    expect(screen.getByText("Continues the task after its caller answered")).toBeTruthy();
    expect(screen.getByText("Which currency?")).toBeTruthy();
    expect(screen.getByText("NZD, 9000")).toBeTruthy();
    const link = screen.getByRole("link", { name: /View the run that asked/ });
    expect(link.getAttribute("href")).toBe("/operations/executions/run-1");
  });

  it("links no run the viewer cannot open", () => {
    renderExchange({ answers: { question: "Which currency?", answer: "NZD", previous_run_id: null } });
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("renders nothing for a run with no exchange", () => {
    const { container } = renderExchange(null);
    expect(container.textContent).toBe("");
  });
});

describe("a2aNoResultText", () => {
  it("explains the missing result by where the question stands", () => {
    expect(a2aNoResultText(waiting, t_i18n)).toMatch(/waiting for its caller/);
    expect(
      a2aNoResultText({ asked: { question: "q", state: "answered", answered_by_run_id: null } }, t_i18n),
    ).toMatch(/continued the task in another run/);
    expect(
      a2aNoResultText({ asked: { question: "q", state: "withdrawn", answered_by_run_id: null } }, t_i18n),
    ).toMatch(/cancelled before its caller answered/);
  });

  it("leaves any other run to the plain wording", () => {
    expect(a2aNoResultText(null, t_i18n)).toBeNull();
    expect(a2aNoResultText({ answers: { question: "q", answer: "a", previous_run_id: null } }, t_i18n)).toBeNull();
  });
});

describe("runStatusConfig", () => {
  it("reads a completed run whose task waits on its caller as waiting, not completed", () => {
    expect(runStatusConfig({ status: "completed", a2a_exchange: waiting }).label).toBe("Waiting on caller");
  });

  it("reads it as completed once the question is answered", () => {
    expect(
      runStatusConfig({
        status: "completed",
        a2a_exchange: { asked: { question: "q", state: "answered", answered_by_run_id: "run-2" } },
      }).label,
    ).toBe("Completed");
  });
});
