/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What the Fleets tab hands to the assistant, the way
 * `video-studio/video-handoff.ts` and `presentation/assistant-handoff.ts` do:
 * the built-in tool an agent must carry, and the sentence primed in the
 * composer (`primeChatDraft`) that the user finishes and sends.
 */

/**
 * What an agent must carry to create fleets (`useToolAgents`): the platform
 * group with its `create_fleet` function. An agent still storing the
 * former `fleets` id carries it through the alias.
 */
export const FLEETS_JOB = { suite: "platform", fn: "create_fleet" } as const;

/**
 * The first message of the fleet setup interview (#11250), sent on arrival by
 * the "Create with a prompt" action: the assistant reads the steps with its
 * `get_fleet_setup` function and asks what the fleet is for, its agents, its
 * orchestrator, workspace, integrations and knowledge, one question at a
 * time, then proposes the configuration and creates it once the user
 * confirmed. The state is the fleet's: asking again later resumes.
 */
export function createFleetPrompt(t: (text: string) => string): string {
  return t(
    "Help me set up a new fleet. Ask me one question at a time what it is for, which agents it needs, its orchestrator, workspace, integrations and knowledge, then propose the configuration and create it once I confirm.",
  );
}

/**
 * What an agent must carry to change a fleet (#10162, "Modify with AI" on the
 * fleet page): the platform group with its `update_fleet` function, which comes
 * with the other fleet functions (members, exposure, automations, documentation).
 */
export const FLEET_EDIT_JOB = { suite: "platform", fn: "update_fleet" } as const;

/** Why an AI fleet action is off, written under the button (AI off, or nobody carries the tools); `null` when it is on. */
export function fleetPromptReason(
  t: (text: string) => string,
  { aiOff, aiReason, none }: { aiOff: boolean; aiReason: string | null | undefined; none: boolean },
): string | null {
  if (aiOff) return aiReason ?? null;
  if (none) {
    return t(
      "No assistant carries the Platform tool with its fleet functions yet: turn it on in an agent's tools or the General Assistant's.",
    );
  }
  return null;
}
