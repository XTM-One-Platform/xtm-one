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

// What the extractor card sends to preview an extractor on pasted text.
//
// An `agent_output` source reads the agent's answer, so the text is the answer. A
// `tool_output` / `tool_input` source reads the last successful call to its tool among the
// result's trace steps, so the text becomes one such step, in the shape a run records it:
// the preview then goes through the same source resolution as a scored entry.

import type { ExtractorPreviewSample, ExtractorSpec } from "@/types";

export function previewSample(source: ExtractorSpec["source"], text: string): ExtractorPreviewSample {
  if (source.kind === "agent_output") return { agent_output: text };
  const field = source.kind === "tool_output" ? "tool_output" : "tool_input";
  return {
    traces: [{ type: "tool", tool_name: source.tool_name ?? "", outcome: "success", [field]: text }],
  };
}

/** Whether a source names everything its preview needs: a tool source needs its tool. */
export function sourceIsPreviewable(source: ExtractorSpec["source"]): boolean {
  return source.kind === "agent_output" || !!source.tool_name?.trim();
}
