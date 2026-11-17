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
 * The text of a chat message that carries one investigation block.
 *
 * A composer-run investigation stores its report as the message text (the
 * agent's later turns, the channels and the `deep_investigation` tool read it
 * there), while the block draws the same report as the last action of its
 * plan. Shown both ways the reader would get it twice, so the text is left out
 * when it IS the run's report (`messageIsRunReport`); anything else - a
 * sentence when no report could be written, an agent's own answer around a
 * run it called - is shown as always. Decided once the run is read: the block
 * is loading meanwhile, and a run that cannot be read shows the text.
 */
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { useInvestigation } from "@/hooks/api/useInvestigationApi";

import { messageIsRunReport } from "./goal-state";

export function InvestigationMessageText({
  text,
  investigationId,
}: {
  text: string;
  investigationId: string;
}) {
  const { data: run, isError } = useInvestigation(investigationId);
  if (!text.trim()) return null;
  if (!run && !isError) return null;
  if (run?.goal_plan && messageIsRunReport(text, run.report)) return null;
  return <ChatMarkdown content={text} />;
}
