// A run's badges explain themselves to a keyboard user too: each is a tab stop
// whose tooltip opens on focus, on every page that lists or compares runs.

import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { AutomaticRunsPausedBadge } from "./automatic-runs-badge";
import { PlaceboArmTag } from "./PlaceboArmTag";
import { PlatformErrorBadge } from "./platform-error-badge";
import { PromptGuardBadge } from "./prompt-guard-badge";
import { RunModeBadge } from "./run-mode-badge";
import { NeededApprovalBadge, ToolApprovalBadge } from "./tool-approval-badge";

describe("run badges", () => {
  it.each<[string, ReactElement, string, string]>([
    ["Prompt guard", <PromptGuardBadge run={{ apply_prompt_guards: true }} />, "Prompt guard", "prompt guard checked this run's prompts"],
    ["Tool approval", <ToolApprovalBadge run={{ require_tool_approval: true }} />, "Tool approval", "Tool calls that need approval were held"],
    ["Needed approval", <NeededApprovalBadge tools={["create_ticket"]} />, "Needed approval", "Held for approval: create_ticket."],
    ["LLM Only", <RunModeBadge run={{ execution_mode: "llm_only" }} />, "LLM Only", "Only the agent's persona and the entry were sent"],
    ["platform errors", <PlatformErrorBadge run={{ platform_error_count: 2 }} />, "2 left out", "could not run the agent on 2 entries"],
    [
      "automatic runs paused",
      <AutomaticRunsPausedBadge evaluation={{ enabled: false, trigger_config: { schedule: "0 0 * * 1", on_agent_change: false } }} />,
      "Automatic runs paused",
      "The schedule is paused",
    ],
    [
      "a placebo arm",
      <PlaceboArmTag run={{ arm: "no_target", placebo_target: { kind: "persona", skill_id: null }, placebo_comparison_id: "c-1" }} />,
      "No target",
      "The agent with this target removed.",
    ],
    ["the real arm", <PlaceboArmTag run={{ arm: "real", placebo_target: null, placebo_comparison_id: "c-1" }} />, "Real", "The agent as configured"],
  ])("explains %s on focus, reached with Tab", async (_name, badge, text, explanation) => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(badge);
    const trigger = container.querySelector('[tabindex="0"]');
    expect(trigger?.textContent).toContain(text);

    await user.tab();
    expect(document.activeElement).toBe(trigger);
    expect((await screen.findByRole("tooltip")).textContent).toContain(explanation);
  });
});
