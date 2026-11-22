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
 * A flow run's Ask-user park on the shared card (#842, S6;
 * dev-docs/human-gates.md sections 2 and 3). A node with options shows them
 * as tiles, the recommended one marked, the deadline in the reader's time
 * zone; a node without options asks for free text, as the old answer box did.
 * The answer goes through the flow run's respond endpoint: the option key
 * (followed by the note when one is typed), which the backend reads back as
 * `{{ask.option}}`.
 */
import { DecisionCard, type DecisionAnswer, type DecisionView } from "@/components/decisions/DecisionCard";
import { useFlowRunRespond } from "@/hooks/api/useFlowApi";
import type { WorkItemAsk } from "@/types";

/** A flow park as the shared card reads it. Pure: tests read it. */
export function flowDecisionView(question: string, ask: WorkItemAsk | null | undefined): DecisionView {
  return {
    question: ask?.question?.trim() || question,
    context: (ask?.context ?? "").split("\n"),
    options: ask?.options ?? [],
    recommendation: ask?.recommendation ?? null,
    deadline: ask?.deadline ?? null,
    links: ask?.links ?? [],
  };
}

/** The response the respond endpoint stores: the key, then the note after a colon. Pure. */
export function flowResponse({ option, text }: DecisionAnswer): string {
  const note = text?.trim() ?? "";
  if (option) return note ? `${option}: ${note}` : option;
  return note;
}

export function FlowDecisionCard({
  runId,
  question,
  ask,
}: {
  runId: string;
  question: string;
  ask?: WorkItemAsk | null;
}) {
  const respond = useFlowRunRespond();
  const send = (answer: DecisionAnswer) => {
    const response = flowResponse(answer);
    if (!response || respond.isPending) return;
    respond.mutate({ runId, response });
  };
  return (
    <li className="mb-1 list-none" data-testid="flow-decision">
      <DecisionCard
        view={flowDecisionView(question, ask)}
        canAnswer
        pending={respond.isPending}
        onAnswer={send}
        testId="flow-decision"
      />
    </li>
  );
}
