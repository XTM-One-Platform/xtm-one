// The run comparison explains its values, columns and timeline in place.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import EvaluationRunCompare from "./EvaluationRunCompare";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

function runDetail(id: string, model: string, score: number) {
  return {
    id,
    evaluation_id: "e-1",
    agent_id: "a-1",
    agent_name: "Alert Triage Analyst",
    agent_slug: "alert-triage-analyst",
    model_id: model,
    status: "completed",
    score,
    quality_score: null,
    performance_score: null,
    speed_score: null,
    overall_score: null,
    f1_score: null,
    scorer_selection: null,
    quality_source: "judge",
    is_baseline: false,
    results: [
      {
        id: `${id}-res`,
        run_id: id,
        entry_id: "en-1",
        description: "Triage the phishing alert",
        input_prompt: "",
        expected_answer: "Escalate",
        agent_output: "Escalate it",
        score,
        duration_seconds: 2.5,
        execution_details: {
          agent_duration_ms: 2500,
          judge_duration_ms: 400,
          total_duration_ms: 2900,
          ttft_ms: 300,
          traces: [
            { type: "llm", description: "Model call", duration_ms: 2500 },
            { type: "judge_skipped", description: "Judge scoring skipped", duration_ms: 0 },
          ],
        },
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve({ id: "e-1", name: "Alert triage", quality_source: "judge" });
    if (path.startsWith("/evaluations/runs/diff")) {
      return Promise.resolve({ changed: [{ key: "model", value_a: "gpt-x", value_b: "gpt-y" }], identical: [] });
    }
    if (path === "/evaluations/runs/r-1") return Promise.resolve(runDetail("r-1", "gpt-x", 81));
    if (path === "/evaluations/runs/r-2") return Promise.resolve(runDetail("r-2", "gpt-y", 62));
    return Promise.resolve([]);
  }) as typeof api.get);
});

function renderPage() {
  renderWithProviders(
    <MemoryRouter initialEntries={["/evaluations/e-1/compare?runs=r-1,r-2"]}>
      <Routes>
        <Route path="/evaluations/:evaluationId/compare" element={<EvaluationRunCompare />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("EvaluationRunCompare", () => {
  it("names the score a legend value is, then defines it", async () => {
    renderPage();

    fireEvent.focus((await screen.findAllByText("81"))[0]);
    const tip = (await screen.findByRole("tooltip")).textContent;
    expect(tip).toMatch(/^Judge Quality: /);
  });

  it("opens an entry from the keyboard and explains its time to first token", async () => {
    const user = userEvent.setup();
    renderPage();

    const toggle = await screen.findByRole("button", { name: "Entry 1: show the answers" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.focus(screen.getAllByText("TTFT 300ms")[0]);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Time to first token");
  });

  it("says T+ is a running sum of the steps' durations", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Entry 1: show the answers" }));

    fireEvent.focus(screen.getAllByText("T+0ms")[0]);
    expect((await screen.findByRole("tooltip")).textContent).toContain("adding up the durations of the steps before it, not read from a clock");
  });

  it("explains a step where the judge was skipped", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Entry 1: show the answers" }));

    fireEvent.focus(screen.getAllByText("JUDGE_SKIPPED")[0]);
    expect((await screen.findByRole("tooltip")).textContent).toContain("The LLM judge did not score this entry");
  });

  it("names which run each column of the parameter diff is", async () => {
    renderPage();

    expect(await screen.findByText("Parameter Diff")).toBeTruthy();
    expect(screen.getByText("Parameter")).toBeTruthy();
    expect(screen.getAllByText("Alert Triage Analyst · gpt-x").length).toBeGreaterThan(0);
    fireEvent.focus(screen.getByText("Parameter Diff"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("The conditions each run logged when it started");
  });

  it("compares the judge's score on an evaluation still set to the retired F1", async () => {
    // An older row, an import or a copy: its runs may hold an F1, which no new run records.
    const withF1 = (run: ReturnType<typeof runDetail>, f1: number) => ({
      ...run,
      f1_score: f1,
      quality_source: "f1",
      results: run.results.map((result) => ({ ...result, f1_score: f1 })),
    });
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve({ id: "e-1", name: "Alert triage", quality_source: "f1" });
      if (path.startsWith("/evaluations/runs/diff")) return Promise.resolve({ changed: [], identical: [] });
      if (path === "/evaluations/runs/r-1") return Promise.resolve(withF1(runDetail("r-1", "gpt-x", 81), 33));
      if (path === "/evaluations/runs/r-2") return Promise.resolve(withF1(runDetail("r-2", "gpt-y", 62), 44));
      return Promise.resolve([]);
    }) as typeof api.get);
    renderPage();

    expect((await screen.findAllByText("81")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("62").length).toBeGreaterThan(0);
    expect(screen.queryByText("33")).toBeNull();
    expect(screen.queryByText("44")).toBeNull();
    expect(screen.getAllByText("Judge Score").length).toBe(2);
    expect(screen.queryByText("F1 (legacy)")).toBeNull();
    fireEvent.focus(screen.getAllByText("81")[0]);
    expect((await screen.findByRole("tooltip")).textContent).toMatch(/^Judge Quality: /);
  });
});
