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
 * Ask the agent (#9437 slice 4, every item since #9879): Explain, Re-check,
 * It's solved and Do it open a new chat with the item's owner agent (its
 * creator when the owner cannot be opened), the message naming the item
 * ready in the composer. Never sent on its own: the title is agent-written,
 * so the person reads the message, adds their own words and sends it
 * (`primeChatDraft` without `autoSend`). Shown on every item that is not
 * done or cancelled: an item waiting for the reader's decision words the
 * prompts around that decision, any other item around where it stands.
 */
import { useId } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCheck, CircleHelp, RefreshCw, Wand2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { primeChatDraft } from "@/lib/chat-launch";
import { formatTimeAgo } from "@/lib/relative-time";
import { waitsForAnswer } from "@/lib/work-item-context";
import type { WorkItem, WorkItemAgentRef } from "@/types";

/** The agent a question about the item goes to: its owner, else its creator, when the reader may open it. */
export function askableAgent(item: WorkItem): WorkItemAgentRef | null {
  const candidates = [item.context?.owner_agent, item.context?.created_by_agent];
  return candidates.find((agent): agent is WorkItemAgentRef => !!agent?.accessible && !!agent.id) ?? null;
}

/** Whether Ask the agent is offered: every item but a finished one. */
export function offersAskTheAgent(item: Pick<WorkItem, "status">): boolean {
  return item.status !== "done" && item.status !== "cancelled";
}

export type AskAgentAction = "explain" | "recheck" | "solved" | "do";

type Translate = (text: string, values?: Record<string, string>) => string;

/** The message each action puts in the composer. */
export function askAgentPrompt(t_i18n: Translate, item: Pick<WorkItem, "id" | "title" | "needs_human" | "status">, action: AskAgentAction): string {
  const ref = { id: item.id, title: item.title };
  const deciding = waitsForAnswer(item);
  switch (action) {
    case "explain":
      return deciding
        ? t_i18n(
            "Explain in plain words what you need from me on work item {id} (\"{title}\"): the decision, the options and what each one changes. Read the item first.",
            ref,
          )
        : t_i18n(
            "Explain in plain words where work item {id} (\"{title}\") stands: what is done, what it waits for and what happens next. Read the item first.",
            ref,
          );
    case "recheck":
      return deciding
        ? t_i18n(
            "Re-check work item {id} (\"{title}\") live: is my decision still needed? If it is already solved, close it as obsolete with a note saying why; otherwise update its question and tell me what changed.",
            ref,
          )
        : t_i18n(
            "Re-check work item {id} (\"{title}\") live: is what it waits for still pending? If it is already solved, close it with a note saying why; otherwise update it and tell me what changed.",
            ref,
          );
    case "solved":
      return t_i18n(
        "Work item {id} (\"{title}\") is solved on my side: [what changed]. Verify it live, then close it with a note, or tell me what is still missing.",
        ref,
      );
    case "do":
      return deciding
        ? t_i18n(
            "Handle work item {id} (\"{title}\") yourself: take the recommended option, or the best one if none is recommended, act on it, and record what you did on the item.",
            ref,
          )
        : t_i18n("Handle work item {id} (\"{title}\") yourself now: act on it and record what you did on the item.", ref);
  }
}

/** Opens the agent's chat with *action*'s message ready; false when no agent can be opened. */
export function useAskTheAgent() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  return (item: WorkItem, action: AskAgentAction): boolean => {
    const agent = askableAgent(item);
    if (!agent?.id) return false;
    primeChatDraft(askAgentPrompt(t_i18n, item, action), { agentId: agent.id });
    navigate("/chat");
    return true;
  };
}

export function WorkItemAskAgent({ item, bordered = true }: { item: WorkItem; bordered?: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const ask = useAskTheAgent();
  const agent = askableAgent(item);
  const hasAgent = !!(item.context?.owner_agent || item.context?.created_by_agent);
  const labelId = useId();
  const actions = [
    {
      key: "explain" as const,
      icon: CircleHelp,
      label: t_i18n("Explain"),
      tip: t_i18n("The agent says where the item stands and what it waits for."),
    },
    {
      key: "recheck" as const,
      icon: RefreshCw,
      label: t_i18n("Re-check"),
      tip: t_i18n("The agent checks live whether the wait is over, and closes the item if it is."),
    },
    {
      key: "solved" as const,
      icon: CheckCheck,
      label: t_i18n("It's solved"),
      tip: t_i18n("You tell the agent the problem is gone: it verifies, then closes the item or says what is missing."),
    },
    {
      key: "do" as const,
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
      {agent ? (
        <>
          <div role="group" aria-labelledby={labelId} className="flex flex-wrap gap-2">
            {actions.map(({ key, icon: Icon, label, tip }) => (
              <Tip key={key} content={tip}>
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1"
                  onClick={() => ask(item, key)}
                  data-testid={`work-item-ask-agent-${key}`}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden /> {label}
                </Button>
              </Tip>
            ))}
          </div>
          <p className="content-compact text-default-secondary">
            {t_i18n("Opens a chat with {agent}, the message ready for you to complete and send.", { agent: agent.name ?? "" })}
          </p>
        </>
      ) : (
        <p className="content-compact text-default-secondary">
          {hasAgent
            ? t_i18n("You cannot open the agent behind this work item, so you cannot ask it from here.")
            : t_i18n("No agent owns or created this work item, so there is no agent to ask.")}
        </p>
      )}
    </div>
  );
}
