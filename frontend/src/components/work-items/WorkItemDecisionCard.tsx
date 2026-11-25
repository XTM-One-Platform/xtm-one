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
 * The decision a work item waits for, at the top of its detail (#9437,
 * slice 3b): the question, one button per option (the recommended one
 * filled and marked), what each option does, the deadline, and a free-text
 * answer. Answering records the answer on the item, clears "waiting for
 * you" and hands the item back to its owner agent (`POST /answer`).
 *
 * The card is written for a person (#776): the headline is the ask's
 * question, never the agent's `blocked_reason`; the ask's `context` lines
 * follow it; the deadline is shown in the reader's time zone. The
 * `blocked_reason` (ids, keys, timestamps an agent wrote for itself) is kept
 * under a collapsed "Technical details". A legacy item (no structured `ask`)
 * is headed by its title and takes a free-text answer only.
 *
 * #834: the ask's sources (`ask.links`: the Jira ticket, the pull request,
 * the document) are buttons under the question; the context is Markdown
 * (links clickable, one paragraph per line); the recommended option comes
 * first. An item whose ask is missing or incomplete shows the server's
 * `card_fallback` (the first lines of its brief and the links found in it)
 * and says the card is incomplete, never the bare title alone.
 *
 * Ask the agent (slice 4, `WorkItemAskAgent`) closes the card; the detail
 * dialog shows it on every other unfinished item too (#9879). A sent answer
 * or ask closes the dialog (*onAnswered*, #774).
 *
 * #842 S3: the layout is the shared `DecisionCard` (one component for every
 * human gate); this file maps a work item to its view and sends the answer.
 */
import { useState } from "react";

import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { DecisionCard, type DecisionAnswer, type DecisionView } from "@/components/decisions/DecisionCard";
import { useToast } from "@/components/ui/toast";
import { WorkItemAskAgent } from "@/components/work-items/WorkItemAskAgent";
import { useAnswerWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import type { WorkItem } from "@/types";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** A work item as the shared card reads it. Pure: tests read it. */
export function workItemDecisionView(item: WorkItem, t_i18n: Translate): DecisionView {
  const ask = item.ask ?? null;
  const title = item.title?.trim();
  const fallback = item.card_fallback ?? null;
  const brief = fallback?.brief?.trim() ?? "";
  const technical = item.blocked_reason?.trim() ?? "";
  const asker = item.context?.owner_agent;
  return {
    question:
      ask?.question?.trim() ||
      (title ? t_i18n("Decision needed: {title}", { title }) : t_i18n("An agent waits for your decision on this work item.")),
    context: (ask?.context ?? "").split("\n"),
    options: ask?.options ?? [],
    recommendation: ask?.recommendation ?? null,
    deadline: ask?.deadline ?? null,
    links: ask?.links?.length ? ask.links : (fallback?.links ?? []),
    asker: asker?.accessible && asker.name ? asker.name : null,
    technical: technical ? <p className="whitespace-pre-wrap break-words">{technical}</p> : undefined,
    notice: fallback ? (
      <div className="space-y-1" data-testid="work-item-decision-fallback">
        {!ask?.question && brief && (
          <ChatMarkdown content={brief} className="chat-markdown content-compact text-default-secondary" remoteImages={false} />
        )}
        <p className="content-compact font-medium text-default-primary" data-testid="work-item-decision-incomplete">
          {t_i18n("This card is incomplete: its agent is asked to rewrite it.")}
        </p>
      </div>
    ) : undefined,
  };
}

export function WorkItemDecisionCard({
  item,
  canAnswer,
  onAnswered,
  readOnlyNote,
}: {
  item: WorkItem;
  canAnswer: boolean;
  /** Shown when the person cannot answer; defaults to the company-managed note. */
  readOnlyNote?: string;
  /** Runs once the answer or an ask left: the detail dialog closes. */
  onAnswered?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const answer = useAnswerWorkItem();
  // Remounts the card after a sent answer, so the typed words are cleared.
  const [round, setRound] = useState(0);

  const send = async ({ option, text }: DecisionAnswer) => {
    if (!option && !text) return;
    try {
      await answer.mutateAsync({ id: item.id, option, text });
      setRound((n) => n + 1);
      toast({
        title: t_i18n("Answer sent"),
        description: t_i18n("The work item is back with its agent, with your answer."),
        variant: "success",
      });
      onAnswered?.();
    } catch (e) {
      // The backend detail is untranslated: log it, show a translated hint.
      console.error("work item answer failed", e);
      toast({
        title: t_i18n("Failed to send the answer"),
        description: t_i18n("Try again, or reload the item if it changed."),
        variant: "error",
      });
    }
  };

  return (
    <DecisionCard
      key={round}
      view={workItemDecisionView(item, t_i18n)}
      canAnswer={canAnswer}
      pending={answer.isPending}
      onAnswer={(a) => void send(a)}
      readOnlyNote={readOnlyNote ?? t_i18n("Only an administrator can answer a company-managed work item.")}
      testId="work-item-decision"
    >
      <WorkItemAskAgent item={item} onDone={onAnswered} />
    </DecisionCard>
  );
}
