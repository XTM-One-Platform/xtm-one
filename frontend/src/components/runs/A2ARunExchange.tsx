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

import { Icon, type IconName } from "@filigran/design-system";
import { Link } from "react-router-dom";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { A2AExchange } from "@/types";

/** One labelled block of the exchange: what was asked, or what was answered. */
function ExchangeText({ label, content }: { label: string; content: string }) {
  return (
    <div className="rounded-sm bg-elevation-hover p-3">
      <p className="content-caption text-default-secondary uppercase tracking-wider mb-1">{label}</p>
      <div className="content-compact text-default-secondary">
        <ChatMarkdown content={content} />
      </div>
    </div>
  );
}

function RunLink({ runId, label, icon }: { runId: string; label: string; icon: IconName }) {
  return (
    <Button asChild variant="secondary" size="sm" className="gap-1.5">
      <Link to={`/operations/executions/${runId}`}>
        <Icon name={icon} size={16} /> {label}
      </Link>
    </Button>
  );
}

/**
 * An external run's place in its A2A task's exchange with the caller
 * (`AssignmentRunDetail.a2a_exchange`): the question this run asked and where
 * it stands, and, for a run the caller's answer started, what it answers. The
 * two runs of one exchange link to each other. Renders nothing on any other run.
 *
 * The caller is the partner AGENT that submitted the task: nobody here answers
 * it, so this card offers no form, unlike a run awaiting a person's input.
 */
export function A2ARunExchange({ exchange }: { exchange: A2AExchange | null | undefined }) {
  const { t_i18n } = useFormatter();
  const asked = exchange?.asked;
  const answers = exchange?.answers;
  if (!asked && !answers) return null;
  const waiting = asked?.state === "waiting";

  return (
    <>
      {answers && (
        <Card data-testid="a2a-exchange-answers">
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2">
              <Icon name="message-square-text" size={16} className="text-accent" />
              {t_i18n("Continues the task after its caller answered")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <ExchangeText label={t_i18n("The agent asked")} content={answers.question} />
            <ExchangeText label={t_i18n("The caller answered")} content={answers.answer} />
            {answers.previous_run_id && (
              <RunLink runId={answers.previous_run_id} icon="arrow-left" label={t_i18n("View the run that asked")} />
            )}
          </CardContent>
        </Card>
      )}
      {asked && (
        <Card
          className={cn(waiting && "border-feedback-warning-primary/30")}
          data-testid="a2a-exchange-asked"
        >
          <CardHeader>
            <CardTitle
              className={cn("content-base flex items-center gap-2", waiting && "text-feedback-warning-primary")}
            >
              <Icon name="circle-question-mark" size={16} className={cn(!waiting && "text-default-secondary")} />
              {waiting ? t_i18n("Waiting on its caller") : t_i18n("Asked its caller")}
              {asked.state === "answered" && <Badge variant="success">{t_i18n("Answered")}</Badge>}
              {asked.state === "withdrawn" && <Badge variant="outline">{t_i18n("Cancelled before an answer")}</Badge>}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <ExchangeText label={t_i18n("The agent asked")} content={asked.question} />
            {waiting && (
              <p className="content-compact text-default-secondary">
                {t_i18n("The A2A task is waiting for the agent that submitted it to answer. Its answer continues the task in a new run, linked from here.")}
              </p>
            )}
            {asked.answered_by_run_id && (
              <RunLink runId={asked.answered_by_run_id} icon="arrow-right" label={t_i18n("View the run that continued it")} />
            )}
          </CardContent>
        </Card>
      )}
    </>
  );
}
