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
 * An evaluation run's subject, an agent or a flow (#8257): where its link opens.
 * A flow subject is linked by the Platform Health; its run carries `flow_id` and no
 * `agent_id`.
 */
export interface EvaluationSubjectRef {
  agent_id?: string | null;
  flow_id?: string | null;
}

export function evaluationSubjectPath(run: EvaluationSubjectRef): string | null {
  if (run.flow_id) return `/flows/library/${run.flow_id}`;
  if (run.agent_id) return `/agents/${run.agent_id}`;
  return null;
}

export function evaluationSubjectKey(run: EvaluationSubjectRef): string {
  return run.flow_id ?? run.agent_id ?? "";
}
