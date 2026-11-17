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

/** The fields that say whether an evaluation starts runs on its own. */
export interface AutomaticRunsEvaluation {
  enabled: boolean;
  trigger_config: { schedule: string | null; on_agent_change: boolean };
}

/**
 * Whether the evaluation starts runs on its own: a schedule. On agent change is not
 * counted, since it starts no run yet; its switch is hidden until it does.
 */
export function hasAutomaticRuns(evaluation: AutomaticRunsEvaluation): boolean {
  return !!evaluation.trigger_config?.schedule;
}
