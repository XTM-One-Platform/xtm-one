// The agent's Evaluations tab: a launch the server refuses says why, once.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { Agent } from "@/types";
import { EvaluationTab } from "./EvaluationTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const EVALUATION = {
  id: "e-1",
  name: "Alert triage",
  agent_ids: ["agent-1"],
  model_ids: ["gpt-x"],
  dataset_ids: ["d-1"],
  last_triggered_at: null,
  trigger_config: { schedule: null, on_agent_change: false },
};

describe("EvaluationTab", () => {
  it("says once why the server refuses a launch", async () => {
    const reason = "Model gone-model is not offered by any provider of the evaluation's owner";
    vi.mocked(api.get).mockResolvedValue([EVALUATION]);
    vi.mocked(api.post).mockRejectedValue(new Error(reason));
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter>
        <EvaluationTab agent={{ id: "agent-1" } as Agent} canManage />
      </MemoryRouter>,
      { client: testQueryClient({ appMutationCache: true }) },
    );

    await user.click(await screen.findByRole("button", { name: "Run this evaluation now" }));
    expect(await screen.findAllByText(reason)).toHaveLength(1);
    expect(api.post).toHaveBeenCalledWith("/evaluations/e-1/trigger", undefined);
  });
});
