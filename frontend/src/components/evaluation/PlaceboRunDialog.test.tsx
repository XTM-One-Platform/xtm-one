// "Run placebo comparison" (FR-11 to FR-14): the estimate, what refuses it, the write-tools confirmation, what it sends.
// The trigger sends the estimate's seed back, so it builds the placebos the estimate checked.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { PlaceboEstimate } from "@/types";
import { PlaceboRunDialog } from "./PlaceboRunDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => ai.off,
}));

const post = vi.mocked(api.post);

function estimate(over: Partial<PlaceboEstimate> = {}): PlaceboEstimate {
  return {
    seed: "0123456789abcdef0123456789abcdef",
    runs: 5,
    agent_executions: 300,
    judge_calls: 300,
    probe_calls: 40,
    execution_cap: 2000,
    estimated_tokens: 1_200_000,
    skipped: [],
    write_tool_sources: {},
    warnings: [],
    refusals: [],
    ...over,
  };
}

const get = vi.mocked(api.get);

function answer(value: PlaceboEstimate) {
  get.mockImplementation(((path: string) =>
    Promise.resolve(path.startsWith("/skills") ? [{ id: "s-1", name: "Triage Checklist" }] : [])) as typeof api.get);
  post.mockImplementation(((path: string) =>
    Promise.resolve(path.endsWith("/placebo/estimate") ? value : [{ id: "r-1" }, { id: "r-2" }])) as typeof api.post);
}

beforeEach(() => {
  vi.clearAllMocks();
  ai.off = false;
});

function renderDialog(props: { modelIds?: string[]; includeDefault?: boolean } = {}) {
  const onOpenChange = vi.fn();
  renderWithProviders(<PlaceboRunDialog open onOpenChange={onOpenChange} evaluationId="e-1" {...props} />);
  return { onOpenChange };
}

const triggerCalls = () => post.mock.calls.filter(([path]) => path === "/evaluations/e-1/trigger");

describe("PlaceboRunDialog", () => {
  it("shows what the comparison would run, then triggers it as a placebo comparison", async () => {
    answer(estimate());
    const { onOpenChange } = renderDialog();

    expect(await screen.findByText("300 (limit 2,000)")).toBeInTheDocument();
    expect(screen.getByText("40 at most")).toBeInTheDocument();
    expect(screen.getByText((1_200_000).toLocaleString())).toBeInTheDocument();
    expect(post.mock.calls[0]).toEqual(["/evaluations/e-1/placebo/estimate", { include_default: false }]);

    fireEvent.click(screen.getByRole("button", { name: "Run comparison" }));

    await waitFor(() => expect(triggerCalls()).toHaveLength(1));
    expect(triggerCalls()[0]).toEqual(["/evaluations/e-1/trigger", { placebo: true, placebo_seed: "0123456789abcdef0123456789abcdef" }]);
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("explains what each estimated figure counts", async () => {
    answer(estimate());
    renderDialog();

    fireEvent.focus(await screen.findByText("Probe calls"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("measure a placebo's length in its tokens");
    fireEvent.blur(screen.getByText("Probe calls"));
    fireEvent.focus(screen.getByText("Agent executions"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("A comparison above the limit cannot run.");
  });

  it("estimates and runs on the selected models", async () => {
    answer(estimate());
    renderDialog({ modelIds: ["gpt-x"], includeDefault: true });

    await screen.findByText("300 (limit 2,000)");
    fireEvent.click(screen.getByRole("button", { name: "Run comparison" }));

    await waitFor(() => expect(triggerCalls()).toHaveLength(1));
    expect(post.mock.calls[0]).toEqual(["/evaluations/e-1/placebo/estimate", { model_ids: ["gpt-x"], include_default: true }]);
    expect(triggerCalls()[0]).toEqual([
      "/evaluations/e-1/trigger",
      { model_ids: ["gpt-x"], include_default: true, placebo: true, placebo_seed: "0123456789abcdef0123456789abcdef" },
    ]);
  });

  it("cannot run while AI is off", async () => {
    ai.off = true;
    answer(estimate());
    renderDialog();

    await screen.findByText("300 (limit 2,000)");
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeDisabled();
  });

  it("lists every refusal and does not run", async () => {
    answer(estimate({ refusals: ["This comparison would run 2400 agent executions; the limit is 2000"] }));
    renderDialog();

    expect(await screen.findByRole("alert")).toHaveTextContent("This comparison would run 2400 agent executions; the limit is 2000");
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeDisabled();
  });

  it("runs an agent's tools that may change data only once confirmed, and says so", async () => {
    answer(estimate({ write_tool_sources: { "Threat Analyst": ["integration: Mailer", "MCP server: Tickets"] } }));
    renderDialog();

    expect(await screen.findByText("integration: Mailer, MCP server: Tickets", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeDisabled();

    fireEvent.click(screen.getByRole("switch", { name: "Run them anyway" }));
    fireEvent.click(screen.getByRole("button", { name: "Run comparison" }));

    await waitFor(() => expect(triggerCalls()).toHaveLength(1));
    expect(triggerCalls()[0]).toEqual(["/evaluations/e-1/trigger", { placebo: true, confirm_write_tools: true, placebo_seed: "0123456789abcdef0123456789abcdef" }]);
  });

  it("says the token estimate waits for a first measured run, and lists the targets left out", async () => {
    answer(
      estimate({
        estimated_tokens: null,
        skipped: [
          { agent_id: "a-1", agent_name: "Threat Analyst", target: "prompts", reason: "the agent has no attached Prompt" },
          { agent_id: "a-1", agent_name: "Threat Analyst", target: "skill:s-1", reason: "the skill is not bound to the agent" },
        ],
      }),
    );
    renderDialog();

    expect(await screen.findByText("Not measured yet")).toBeInTheDocument();
    expect(screen.getByText("Threat Analyst · Prompts: the agent has no attached Prompt")).toBeInTheDocument();
    // A single-skill target is named by its skill.
    expect(await screen.findByText("Threat Analyst · Skill · Triage Checklist: the skill is not bound to the agent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeEnabled();
  });

  it("estimates afresh at each opening and never launches on an earlier estimate", async () => {
    answer(estimate());
    const { rerender } = renderWithProviders(<PlaceboRunDialog open onOpenChange={vi.fn()} evaluationId="e-1" />);
    await screen.findByText("300 (limit 2,000)");

    answer(estimate({ refusals: ["No agent of this evaluation has any of the placebo targets"] }));
    rerender(<PlaceboRunDialog open={false} onOpenChange={vi.fn()} evaluationId="e-1" />);
    rerender(<PlaceboRunDialog open onOpenChange={vi.fn()} evaluationId="e-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No agent of this evaluation has any of the placebo targets");
    expect(post.mock.calls.filter(([path]) => path === "/evaluations/e-1/placebo/estimate")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeDisabled();
  });

  it("names each warning the comparison would carry", async () => {
    answer(estimate({ warnings: ["sequential_run_mode"] }));
    renderDialog();

    expect(await screen.findByText("The arms ran one after the other, not interleaved")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run comparison" })).toBeEnabled();
  });

  it("says once why the server refuses the launch, and stays open", async () => {
    const reason = "Model gone-model is not offered by any provider of the evaluation's owner";
    answer(estimate());
    post.mockImplementation(((path: string) =>
      path.endsWith("/placebo/estimate") ? Promise.resolve(estimate()) : Promise.reject(new Error(reason))) as typeof api.post);
    const onOpenChange = vi.fn();
    renderWithProviders(<PlaceboRunDialog open onOpenChange={onOpenChange} evaluationId="e-1" />, {
      client: testQueryClient({ appMutationCache: true }),
    });

    expect(await screen.findByText("300 (limit 2,000)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run comparison" }));
    expect(await screen.findAllByText(reason)).toHaveLength(1);
    expect(triggerCalls()).toHaveLength(1);
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
