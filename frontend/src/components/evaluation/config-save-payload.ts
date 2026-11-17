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

import type { CreateEvaluationRequest, EvaluationFamily, ExtractorSpec, FieldScoringOverrides } from "@/types";

/** What the Configuration tab edits, as its form holds it. */
export interface ConfigSaveState {
  name: string;
  description: string;
  family: EvaluationFamily | "";
  evalInstructions: string;
  executionMode: string;
  runMode: "parallel" | "sequential";
  evaluatorModel: string;
  performanceTolerance: number;
  penaltyFactor: number;
  qualitySelection: string[] | null;
  performanceSelection: string[] | null;
  timeoutSeconds: number;
  judgeMaxTokens: number;
  fieldScoringOverrides: FieldScoringOverrides | null;
  agentExtractors: Record<string, ExtractorSpec | null>;
  judgeEnabled: boolean;
  reuseDefault: boolean;
  applyPromptGuards: boolean;
  requireToolApproval: boolean;
  scheduleEnabled: boolean;
  schedule: string;
  onAgentChange: boolean;
  automaticRuns: boolean;
  useAgentDefaultModel: boolean;
  modelIds: string[];
  disabledModelIds: string[];
}

/**
 * The update the Configuration tab's save sends.
 *
 * The update clears a nullable setting sent as `null` and keeps one left out,
 * so `null` is sent only to clear: an empty description, the judge model back
 * to "Platform default", no field override, and a headline selection back to
 * inherit-all. An empty family is left out, never `null`: an evaluation with no
 * family cannot run. `trigger_config` carries only what the tab edits; the
 * update merges it, keeping the callback settings set through the API.
 */
export function configSavePayload(state: ConfigSaveState): Partial<CreateEvaluationRequest> {
  return {
    name: state.name,
    description: state.description || null,
    family: state.family || undefined,
    evaluation_instructions: state.evalInstructions,
    execution_mode: state.executionMode as CreateEvaluationRequest["execution_mode"],
    run_mode: state.runMode,
    evaluator_model: state.evaluatorModel || null,
    performance_tolerance: state.performanceTolerance,
    penalty_factor: state.penaltyFactor,
    overall_quality_metrics: state.qualitySelection,
    overall_performance_metrics: state.performanceSelection,
    timeout_seconds: state.timeoutSeconds,
    judge_max_tokens: state.judgeMaxTokens,
    field_scoring_overrides: state.fieldScoringOverrides,
    // Sent explicitly so a cleared extractor is persisted as a clear.
    agent_extractors: state.agentExtractors,
    // Judge on → ["judge"]; off → explicit [] so the gate reads it as disabled.
    scorer_selection: state.judgeEnabled ? ["judge"] : [],
    reuse_default: state.reuseDefault ? "on" : "off",
    apply_prompt_guards: state.applyPromptGuards,
    require_tool_approval: state.requireToolApproval,
    trigger_config: {
      schedule: state.scheduleEnabled && state.schedule.trim() ? state.schedule.trim() : null,
      on_agent_change: state.onAgentChange,
    },
    enabled: state.automaticRuns,
    use_agent_default_model: state.useAgentDefaultModel,
    model_ids: state.modelIds,
    disabled_model_ids: state.disabledModelIds.filter((mid) => state.modelIds.includes(mid)),
  };
}
