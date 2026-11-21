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
 * Ask the agent (#9437 slice 4, every item since #9879, one rule since #774):
 * an action whose wording the person read on its button is SENT, never left
 * in a composer.
 *
 * - Re-check, It's solved and Do it go to the item's own agent (its owner,
 *   else its creator; an agent built for assignments only included) through
 *   `POST /work-items/{id}/ask`: a note lands on the item, the agent takes it
 *   now, a toast says so and the dialog closes. No chat opens.
 * - Explain opens a chat with that agent, the message sent on arrival, so the
 *   person lands on the answer streaming.
 *
 * Shown on every item that is not done or cancelled.
 */
import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCheck, CircleHelp, RefreshCw, Send, Wand2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useAskWorkItem } from "@/hooks/api/useWorkItemApi";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { primeChatDraft } from "@/lib/chat-launch";
import { formatTimeAgo } from "@/lib/relative-time";
import { waitsForAnswer } from "@/lib/work-item-context";
import type { WorkItem, WorkItemAgentRef } from "@/types";

/** The agent a chat about the item opens with: its owner, else its creator, when the reader may open it. */
function askableAgent(item: WorkItem): WorkItemAgentRef | null {
  const candidates = [item.context?.owner_agent, item.context?.created_by_agent];
  return candidates.find((agent): agent is WorkItemAgentRef => !!agent?.accessible && !!agent.id) ?? null;
}

/** The agent a sent action reaches (the server's pick): the owner, else the creator, seen or not. */
export function sendTarget(item: WorkItem): WorkItemAgentRef | null {
  return item.context?.owner_agent ?? item.context?.created_by_agent ?? null;
}

/** Whether Ask the agent is offered: every item but a finished one. */
export function offersAskTheAgent(item: Pick<WorkItem, "status">): boolean {
  return item.status !== "done" && item.status !== "cancelled";
}

/** How long a send stays pending when nothing moved on the item (the server's window). */
const ASK_PENDING_MS = 10 * 60 * 1000;

/** Whether an earlier send still waits for its agent: sent under ten minutes ago and nothing moved since. */
export function askPending(item: Pick<WorkItem, "payload" | "last_activity_at">, now: number = Date.now()): boolean {
  const sent = (item.payload?._ask ?? null) as { at?: string } | null;
  const at = sent?.at ? Date.parse(sent.at) : NaN;
  if (Number.isNaN(at) || now - at > ASK_PENDING_MS) return false;
  const last = item.last_activity_at ? Date.parse(item.last_activity_at) : NaN;
  return Number.isNaN(last) || last <= at + 1000;
}

type AskAgentAction = "explain" | "recheck" | "solved" | "do";
export type SentAskAction = Exclude<AskAgentAction, "explain">;

type Translate = (text: string, values?: Record<string, string>) => string;

/** The Explain message the chat sends on arrival. */
export function explainPrompt(t_i18n: Translate, item: Pick<WorkItem, "id" | "title" | "needs_human" | "status">): string {
  const ref = { id: item.id, title: item.title };
  return waitsForAnswer(item)
    ? t_i18n(
        "Explain in plain words what you need from me on work item {id} (\"{title}\"): the decision, the options and what each one changes. Read the item first.",
        ref,
      )
    : t_i18n(
        "Explain in plain words where work item {id} (\"{title}\") stands: what is done, what it waits for and what happens next. Read the item first.",
        ref,
      );
}

/**
 * The item's actions as one rule (#774). `explain` opens the agent's chat
 * with the message sent; `send` posts the action to the item's agent and
 * toasts. *onDone* runs once the action left (the dialog closes).
 * The ask route and the chat are AI gated: with AI off both are refused, so
 * `aiOff` disables every control that starts one and `aiReason` says why.
 */
export function useAskTheAgent(onDone?: () => void) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const ask = useAskWorkItem();
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();

  const explain = (item: WorkItem): boolean => {
    if (aiOff) return false;
    const agent = askableAgent(item);
    if (!agent?.id) return false;
    primeChatDraft(explainPrompt(t_i18n, item), { agentId: agent.id, autoSend: true });
    onDone?.();
    navigate("/chat");
    return true;
  };

  const send = async (item: WorkItem, action: SentAskAction, text?: string): Promise<boolean> => {
    if (aiOff) return false;
    try {
      const sent = await ask.mutateAsync({ id: item.id, action, text: text?.trim() || undefined });
      toast({
        title: t_i18n("Sent to {agent}", { agent: sent.agent.name }),
        description: t_i18n("It takes the work item now and records the outcome on it."),
        variant: "success",
      });
      onDone?.();
      return true;
    } catch (e) {
      // The backend detail is untranslated: log it, show a translated hint.
      console.error("work item ask failed", e);
      const already = e instanceof Error && e.message.includes("already sent");
      toast({
        title: already ? t_i18n("Already sent") : t_i18n("Not sent"),
        description: already
          ? t_i18n("The agent has not picked up the last request yet. Wait for its note on the work item.")
          : t_i18n("Reload the work item and try again. A disabled agent must be enabled first."),
        variant: "error",
      });
      return false;
    }
  };

  return { explain, send, isPending: ask.isPending, aiOff, aiReason };
}

export function WorkItemAskAgent({
  item,
  bordered = true,
  onDone,
}: {
  item: WorkItem;
  bordered?: boolean;
  /** Runs once an action left: the detail dialog closes. */
  onDone?: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { explain, send, isPending, aiOff, aiReason } = useAskTheAgent(onDone);
  const [solving, setSolving] = useState(false);
  const [changed, setChanged] = useState("");
  const chatAgent = askableAgent(item);
  const target = sendTarget(item);
  const agentName = target?.name || t_i18n("its agent");
  const pending = askPending(item);
  const labelId = useId();
  const changedId = useId();
  const disabled = pending || isPending || aiOff;

  const sent: { key: SentAskAction; icon: typeof RefreshCw; label: string; tip: string }[] = [
    {
      key: "recheck",
      icon: RefreshCw,
      label: t_i18n("Re-check"),
      tip: t_i18n("The agent checks live whether the wait is over, and closes the item if it is."),
    },
    {
      key: "solved",
      icon: CheckCheck,
      label: t_i18n("It's solved"),
      tip: t_i18n("You tell the agent the problem is gone: it verifies, then closes the item or says what is missing."),
    },
    {
      key: "do",
      icon: Wand2,
      label: t_i18n("Do it"),
      tip: t_i18n("The agent acts on the item now and records what it did."),
    },
  ];

  return (
    <div className={bordered ? "space-y-1.5 border-t border-accent/20 pt-3" : "space-y-1.5"} data-testid="work-item-ask-agent">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span id={labelId} className="content-compact font-medium text-default-primary">
          {t_i18n("Ask the agent")}
        </span>
        {item.last_activity_at && (
          <span className="content-compact ml-auto text-default-secondary" data-testid="work-item-ask-freshness">
            {t_i18n("Last activity {time}", { time: formatTimeAgo(intl, item.last_activity_at) })}
          </span>
        )}
      </div>
      {target ? (
        <>
          <div role="group" aria-labelledby={labelId} className="flex flex-wrap gap-2">
            {chatAgent && (
              <Tip
                content={aiReason ?? t_i18n("Opens a chat with {agent} and asks where the item stands.", { agent: chatAgent.name ?? "" })}
                wrap={aiOff}
              >
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1"
                  disabled={aiOff}
                  onClick={() => explain(item)}
                  data-testid="work-item-ask-agent-explain"
                >
                  <CircleHelp className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Explain")}
                </Button>
              </Tip>
            )}
            {sent.map(({ key, icon: Icon, label, tip }) => (
              <Tip key={key} content={aiReason ?? tip} wrap={aiOff}>
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1"
                  disabled={disabled}
                  aria-expanded={key === "solved" ? solving : undefined}
                  onClick={() => (key === "solved" ? setSolving((open) => !open) : void send(item, key))}
                  data-testid={`work-item-ask-agent-${key}`}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden /> {label}
                </Button>
              </Tip>
            ))}
          </div>
          {solving && !disabled && (
            <div className="space-y-1.5" data-testid="work-item-ask-solved-form">
              <Label htmlFor={changedId} className="text-default-secondary">
                {t_i18n("What changed (optional)")}
              </Label>
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <Input
                    id={changedId}
                    aria-label={t_i18n("What changed (optional)")}
                    value={changed}
                    maxLength={500}
                    onChange={(e) => setChanged(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void send(item, "solved", changed);
                    }}
                  />
                </div>
                <Button
                  className="gap-1"
                  disabled={aiOff}
                  onClick={() => void send(item, "solved", changed)}
                  data-testid="work-item-ask-solved-send"
                >
                  <Send className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Send")}
                </Button>
              </div>
            </div>
          )}
          <p className="content-compact text-default-secondary" data-testid="work-item-ask-effect">
            {pending
              ? t_i18n("Already sent, waiting for {agent}.", { agent: agentName })
              : t_i18n("Re-check, It's solved and Do it are sent to {agent}, which records the outcome on the item.", {
                  agent: agentName,
                })}
          </p>
          {!chatAgent && (
            <p className="content-compact text-default-secondary">
              {t_i18n("You cannot open the agent behind this work item, so Explain is not offered.")}
            </p>
          )}
        </>
      ) : (
        <p className="content-compact text-default-secondary">
          {t_i18n("No agent owns or created this work item, so there is no agent to ask.")}
        </p>
      )}
    </div>
  );
}
