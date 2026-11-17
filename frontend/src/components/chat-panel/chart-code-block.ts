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
 * The chart a stored answer carries, as a fenced code block draws: a
 * `xtm-chart` fence the chat draws is the chart, any other stays the code it
 * is. `XtmChatPanel` passes it as the panel's `codeBlockRenderers`;
 * `ChatMarkdown` draws the same fence with it.
 */
import { Suspense, createElement, lazy, type ReactNode } from "react";
import { CHART_FENCE_LANGUAGE, parseChartBlock } from "@/components/chat/chat-chart";

// recharts loads with the first chart an answer carries, not with the chat.
const ChatChart = lazy(() => import("@/components/chat/ChatChart"));

/** Holds the chart's place while its chunk loads. */
const CHART_PLACEHOLDER = createElement("div", {
  className: "my-4 h-[280px] rounded-sm border border-dashed border-elevation-subtle-soft",
});

/** The chart of a `xtm-chart` fence, or `null` when the block is not one the chat draws (the panel shows the code). */
export function chartCodeBlockRenderer(code: string): ReactNode {
  const block = parseChartBlock(code);
  if (!block) return null;
  return createElement(Suspense, { fallback: CHART_PLACEHOLDER }, createElement(ChatChart, { block }));
}

/** The code-block renderers the panel takes, by fence language. */
export const CHART_CODE_BLOCK_RENDERERS: Record<string, (code: string) => ReactNode> = {
  [CHART_FENCE_LANGUAGE]: chartCodeBlockRenderer,
};
