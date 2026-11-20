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
 * Ask the agent (slice 4, `WorkItemAskAgent`) closes the card; the detail
 * dialog shows it on every other unfinished item too (#9879).
 */
import { useId, useState } from "react";
import { CalendarClock, MessageSquareReply, Send, Sparkles } from "lucide-react";
import { Textarea } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { WorkItemAskAgent } from "@/components/work-items/WorkItemAskAgent";
import { useAnswerWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import type { WorkItem, WorkItemAskOption } from "@/types";

export function WorkItemDecisionCard({ item, canAnswer }: { item: WorkItem; canAnswer: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const answer = useAnswerWorkItem();
  const [text, setText] = useState("");
  const textId = useId();
  const textLabel = (item.ask?.options?.length ?? 0) > 0 ? t_i18n("Or answer in your own words") : t_i18n("Your answer");
  const ask = item.ask ?? null;
  const title = item.title?.trim();
  const question =
    ask?.question?.trim() ||
    (title ? t_i18n("Decision needed: {title}", { title }) : t_i18n("An agent waits for your decision on this work item."));
  const context = (ask?.context ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const technical = item.blocked_reason?.trim() ?? "";
  const options = ask?.options ?? [];
  const recommended = ask?.recommendation ?? null;

  const send = async (option?: WorkItemAskOption) => {
    const words = text.trim();
    if (!option && !words) return;
    try {
      await answer.mutateAsync({ id: item.id, option: option?.key, text: words || undefined });
      setText("");
      toast({
        title: t_i18n("Answer sent"),
        description: t_i18n("The work item is back with its agent, with your answer."),
        variant: "success",
      });
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
    <section
      className="space-y-3 rounded-sm border border-accent/40 bg-accent/5 p-3"
      aria-labelledby={`${textId}-question`}
      data-testid="work-item-decision"
    >
      <div className="flex flex-wrap items-center gap-2">
        <MessageSquareReply className="h-4 w-4 shrink-0 text-accent" aria-hidden />
        <span className="content-caption font-medium uppercase tracking-wider text-default-secondary">
          {t_i18n("Waiting for your decision")}
        </span>
        {ask?.deadline && (
          <Badge variant="secondary" className="content-caption ml-auto gap-1" data-testid="work-item-decision-deadline">
            <CalendarClock className="h-3 w-3" aria-hidden />
            {t_i18n("Answer by {date}", { date: formatShortDateTimeIntl(intl, ask.deadline) })}
          </Badge>
        )}
      </div>
      <p id={`${textId}-question`} className="content-base font-medium text-default-primary">
        {question}
      </p>
      {context.length > 0 && (
        <div className="space-y-1" data-testid="work-item-decision-context">
          {context.map((line, index) => (
            <p key={index} className="content-compact text-default-secondary">
              {line}
            </p>
          ))}
        </div>
      )}

      {options.length > 0 && (
        <ul className="space-y-2" data-testid="work-item-decision-options">
          {options.map((option) => {
            const isRecommended = !!recommended && option.key === recommended;
            return (
              <li key={option.key} className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-1">
                <Button
                  size="sm"
                  variant={isRecommended ? "default" : "outline"}
                  className="h-auto min-h-6 max-w-full gap-1 whitespace-normal py-1 text-left"
                  disabled={!canAnswer || answer.isPending}
                  onClick={() => void send(option)}
                  data-testid="work-item-decision-option"
                >
                  {isRecommended && <Sparkles className="h-3.5 w-3.5 shrink-0" aria-hidden />}
                  {option.label}
                </Button>
                <div className="min-w-0 flex-1 space-y-0.5 pt-1">
                  {isRecommended && (
                    <span className="block content-compact font-medium text-accent">{t_i18n("Recommended")}</span>
                  )}
                  {option.effect && <span className="block content-compact text-default-secondary">{option.effect}</span>}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {canAnswer ? (
        <div className="space-y-1.5">
          <Label htmlFor={textId} className="text-default-secondary">
            {textLabel}
          </Label>
          <Textarea id={textId} aria-label={textLabel} value={text} rows={2} onChange={(e) => setText(e.target.value)} />
          <div className="flex justify-end">
            <Button
              size="sm"
              variant={options.length > 0 ? "outline" : "default"}
              className="gap-1"
              disabled={!text.trim() || answer.isPending}
              onClick={() => void send()}
            >
              <Send className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Send answer")}
            </Button>
          </div>
        </div>
      ) : (
        <p className="content-compact text-default-secondary">
          {t_i18n("Only an administrator can answer a company-managed work item.")}
        </p>
      )}

      {technical && (
        <details data-testid="work-item-decision-technical">
          <DisclosureSummary>{t_i18n("Technical details")}</DisclosureSummary>
          <p className="content-compact whitespace-pre-wrap break-words pt-2 text-default-secondary">{technical}</p>
        </details>
      )}

      <WorkItemAskAgent item={item} />
    </section>
  );
}
