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

import type { CreateEvaluationRequest, ExtractorSpec } from "@/types";

/** The settings the creation screen sets beyond its other cards' fields. */
export interface CreationExtrasState {
  /** The agents the evaluation is created with, in the screen's order. */
  agentIds: string[];
  agentExtractors: Record<string, ExtractorSpec | null>;
  qualitySelection: string[] | null;
  performanceSelection: string[] | null;
}

/**
 * The overall metric selection and each agent's output extraction, as the create request
 * carries them: what a later edit of the Configuration tab would save, read by the
 * same validators. An extractor is sent only for an agent the evaluation is created with,
 * so one set on an agent removed since never reaches the request (the API refuses an
 * extractor for an agent the evaluation does not have). A selection left on its defaults is
 * `null`, which the API reads as inherit.
 */
export function creationExtras(
  state: CreationExtrasState,
): Pick<CreateEvaluationRequest, "agent_extractors" | "overall_quality_metrics" | "overall_performance_metrics"> {
  const extractors = Object.fromEntries(
    state.agentIds.flatMap((id) => {
      const extractor = state.agentExtractors[id];
      return extractor ? [[id, extractor] as const] : [];
    }),
  );
  return {
    agent_extractors: Object.keys(extractors).length > 0 ? extractors : undefined,
    overall_quality_metrics: state.qualitySelection,
    overall_performance_metrics: state.performanceSelection,
  };
}
