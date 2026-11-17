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

import { useMemo } from "react";
import { Eye, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { useAgents, useSubAgentTasks } from "@/hooks/api/useAgentApi";
import { useAwaitedWork, useReturnToSource, useSharedConversations } from "@/hooks/api/useChatApi";
import { conversationAgentId } from "@/lib/general-assistant-stand-in";
import { AwaitedWorkBanner } from "@/pages/chat/AwaitedWorkBanner";
import { BackgroundTasksBanner } from "@/pages/chat/BackgroundTasksBanner";
import { ChannelContinuityBanner } from "@/pages/chat/ChannelContinuityBanner";
import { ConversationRow } from "@/pages/chat/ConversationSidebar";
import type { ConversationDetail, ConversationListItem } from "@/types";

/**
 * Above the thread: what holds for the conversation as a whole - shared with
 * the user read-only, continued in
 * Slack or Teams, handed over by another agent, and the work it waits
 * on (background tasks, #6046).
 */
export function ConversationBanner({
  conversation,
  agentId,
  working,
  aiOff,
  onReturned,
}: {
  conversation: ConversationDetail | undefined;
  /** The agent whose background tasks are shown: the conversation's, else the one picked. */
  agentId: string | null;
  working: boolean;
  aiOff: boolean;
  /** The conversation went back to the agent that handed it over: the panel reads it again for its agent. */
  onReturned: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: agents } = useAgents();
  const returnToSource = useReturnToSource();
  const readOnly = conversation?.access === "read";
  const { data: bgTasks } = useSubAgentTasks(agentId ? { agent_id: agentId, limit: 10 } : {});
  const activeBgTasks = bgTasks?.filter((t) => t.status === "pending" || t.status === "running") ?? [];
  const { data: awaitedWork } = useAwaitedWork(conversation?.id ?? null, !!conversation && !readOnly);
  const transferSourceName =
    conversation?.transfer_source_agent_name ||
    agents?.find((a) => a.id === conversation?.transfer_source_agent_id)?.name ||
    null;

  return (
    <>
      {readOnly && (
        <div className="flex items-center gap-2 border-b border-feedback-alert-primary/20 bg-feedback-alert-primary/5 px-4 py-1.5 content-compact">
          <Eye className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
          <span className="text-default-secondary">
            {conversation?.owner_name
              ? t_i18n("Shared with you by {name}: you can read this conversation but not reply in it.", {
                  name: conversation.owner_name,
                })
              : t_i18n("Shared with you: you can read this conversation but not reply in it.")}
          </span>
        </div>
      )}
      {conversation && !readOnly && (
        <ChannelContinuityBanner
          channel={conversation.channel || "web"}
          webContinuable={conversation.web_continuable !== false}
          continuedIn={conversation.continued_in ?? []}
        />
      )}
      {conversation?.transfer_source_agent_id && (
        <div className="flex items-center gap-2 border-b border-accent/20 bg-accent/5 px-4 py-1.5 content-compact">
          <Undo2 className="h-3.5 w-3.5 text-accent shrink-0" />
          <span className="text-default-secondary">
            {t_i18n("Transferred from")}{" "}
            <span className="font-medium text-default-primary">{transferSourceName || t_i18n("another agent")}</span>
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-6 px-2 content-compact text-accent hover:text-accent/80"
            disabled={returnToSource.isPending || aiOff || readOnly}
            onClick={() => returnToSource.mutate(conversation.id, { onSuccess: onReturned })}
          >
            {t_i18n("Return")}
          </Button>
        </div>
      )}
      <BackgroundTasksBanner activeBgTasks={activeBgTasks} bgTasks={bgTasks} />
      {!working && <AwaitedWorkBanner items={awaitedWork ?? []} />}
    </>
  );
}

/** Whether a shared conversation matches the list's search: its title, its owner or its last message. */
function matchesSearch(conv: ConversationListItem, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return [conv.title, conv.owner_name, conv.last_message_preview].some((text) => !!text?.toLowerCase().includes(q));
}

/**
 * The conversations others shared with the user by name, under the
 * panel's own list, narrowed by its search (`query`, trimmed): none matching,
 * the section is left out.
 */
export function SharedConversationsSection({
  query,
  activeConversationId,
  onSelect,
}: {
  query: string;
  activeConversationId: string | null;
  onSelect: (conversationId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: shared } = useSharedConversations();
  const { data: agents } = useAgents();
  const matching = useMemo(() => (shared ?? []).filter((conv) => matchesSearch(conv, query)), [shared, query]);
  if (matching.length === 0) return null;
  return (
    <div className="p-2 pt-1">
      <p className="px-3 pb-1.5 pt-2 content-caption font-medium uppercase tracking-wide text-default-secondary">
        {t_i18n("Shared with me")}
      </p>
      <div className="space-y-0.5">
        {matching.map((conv) => (
          <ConversationRow
            key={conv.id}
            conv={conv}
            agentName={agents?.find((a) => a.id === conversationAgentId(conv))?.name}
            isActive={activeConversationId === conv.id}
            readOnly
            onSelect={onSelect}
            t_i18n={t_i18n}
          />
        ))}
      </div>
    </div>
  );
}
