import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { EvaluationSettingsCard, type EvaluationSettingsCardProps } from "@/components/evaluation/evaluation-settings-card";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}));

/**
 * The read-only Settings card of a saved evaluation: every setting's
 * state reads Enabled / Disabled, as the rest of the evaluation configuration.
 */

const noop = () => {};

function renderReadOnly(snapshot: Partial<NonNullable<EvaluationSettingsCardProps["evaluationSnapshot"]>>) {
  renderWithProviders(
    <EvaluationSettingsCard
      editing={false}
      name="Eval"
      onNameChange={noop}
      description=""
      onDescriptionChange={noop}
      executionMode="full_loop"
      onExecutionModeChange={noop}
      runMode="parallel"
      onRunModeChange={noop}
      timeoutSeconds={120}
      onTimeoutSecondsChange={noop}
      scheduleEnabled={false}
      onScheduleEnabledChange={noop}
      schedule=""
      onScheduleChange={noop}
      reuseDefault={false}
      onReuseDefaultChange={noop}
      applyPromptGuards={false}
      onApplyPromptGuardsChange={noop}
      requireToolApproval={false}
      onRequireToolApprovalChange={noop}
      agentIds={[]}
      evaluationSnapshot={{
        execution_mode: "full_loop",
        run_mode: "parallel",
        timeout_seconds: 120,
        schedule: null,
        reuse_default: "off",
        apply_prompt_guards: false,
        require_tool_approval: false,
        ...snapshot,
      }}
    />,
  );
}

/** The state shown under a setting's heading. */
const stateOf = (heading: string) => screen.getByText(heading).parentElement?.textContent?.replace(heading, "");

describe("EvaluationSettingsCard, read-only", () => {
  it.each([
    [true, "Enabled"],
    [false, "Disabled"],
  ])("shows prompt guards, tool approval and prediction reuse as %s → %s", (on, state) => {
    renderReadOnly({ apply_prompt_guards: on, require_tool_approval: on, reuse_default: on ? "on" : "off" });

    expect(stateOf("Apply prompt guards")).toBe(state);
    expect(stateOf("Require tool approval")).toBe(state);
    expect(stateOf("Prediction Reuse")).toBe(state);
    expect(screen.queryByText("On")).toBeNull();
    expect(screen.queryByText("Off")).toBeNull();
  });
});
