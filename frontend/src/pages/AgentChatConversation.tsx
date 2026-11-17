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
 * One of an agent's conversations, read from the agent's detail page (its
 * Chats tab): any channel's (web, Slack, Teams, ...), for whoever may read the
 * agent's chats (`can_view_agent_chats`).
 *
 * The transcript is the shared chat panel, read-only: it restores and pages
 * the conversation through the agent detail's own routes
 * (`agentChatPanelEndpoints`), draws its files and cards as the chat does, and
 * offers no way to send, follow a turn or list other conversations.
 */
import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Calendar, MessageSquare, User } from "lucide-react";
import { XtmChatPanel } from "@/components/chat-panel/XtmChatPanel";
import { agentChatPanelEndpoints } from "@/components/chat-panel/endpoints";
import { BackButton } from "@/components/ui/back-button";
import { Badge } from "@/components/ui/badge";
import { ChannelIcon } from "@/components/ui/channel-icon";
import { NotFoundState } from "@/components/ui/not-found-state";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useAgent, useAgentChatSummary } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import { CHANNEL_TYPE_LABELS, ICON_MAP } from "@/lib/constants";
import { formatDateTime } from "@/lib/utils";

/** Namespaces the panel's own browser storage, away from the chat page's. */
const STORAGE_PREFIX = "xtm-agent-chat";

export default function AgentChatConversation() {
  const { t_i18n } = useFormatter();
  const { id: agentId, conversationId } = useParams<{ id: string; conversationId: string }>();
  const navigate = useNavigate();

  const { data: agent, isLoading: agentLoading } = useAgent(agentId || "");
  const { data: detail, isLoading: detailLoading } = useAgentChatSummary(agentId, conversationId);
  const endpoints = useMemo(() => agentChatPanelEndpoints(agentId ?? ""), [agentId]);

  if (agentLoading || detailLoading) return <PageSkeleton cards={0} />;

  if (!agent) {
    return (
      <NotFoundState
        icon={MessageSquare}
        title="Agent not found"
        description="This agent may have been deleted or you don't have access."
        actionLabel="View All Agents"
        onAction={() => navigate("/agents")}
        backTo="/agents"
        backLabel="Back to Agents"
      />
    );
  }

  if (!detail || !conversationId) {
    return (
      <NotFoundState
        icon={MessageSquare}
        title="Conversation not found"
        description="This conversation may have been deleted."
        actionLabel="Back to Chats"
        onAction={() => navigate(`/agents/${agentId}/chats`)}
        backTo={`/agents/${agentId}/chats`}
        backLabel="Back to Chats"
      />
    );
  }

  // The agent's own mark for the panel's static header label.
  const AgentMark = agent.icon ? ICON_MAP[agent.icon] : undefined;

  return (
    <div className="flex flex-col h-[calc(100vh-var(--fds-header-height,68px))] -m-6">
      <div className="shrink-0 px-6 pt-6 pb-4 space-y-2">
        <BackButton to={`/agents/${agentId}/chats`} label="Back to Chats" />

        <div className="flex items-center gap-3">
          <ChannelIcon channel={detail.channel} className="h-5 w-5" />
          <div>
            <h1 className="text-xl font-bold truncate max-w-[600px]">{detail.title}</h1>
            <div className="flex items-center gap-3 text-[11px] text-default-secondary mt-0.5">
              <Badge variant="outline" className="text-[9px] gap-1">
                <ChannelIcon channel={detail.channel} className="h-2.5 w-2.5" />
                {t_i18n(CHANNEL_TYPE_LABELS[detail.channel] ?? detail.channel)}
              </Badge>
              {detail.user_name && (
                <span className="inline-flex items-center gap-1">
                  <User className="h-3 w-3" />
                  {detail.user_name}
                </span>
              )}
              <span className="inline-flex items-center gap-1">
                <MessageSquare className="h-3 w-3" />
                {t_i18n("{count, plural, one {# message} other {# messages}}", { count: detail.message_count })}
              </span>
              <span className="inline-flex items-center gap-1">
                <Calendar className="h-3 w-3" />
                {formatDateTime(detail.created_at)}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 border-t border-elevation-subtle-soft" data-testid="agent-chat-panel">
        <XtmChatPanel
          apiEndpoints={endpoints}
          storageKeyPrefix={STORAGE_PREFIX}
          conversationId={conversationId}
          // No agent list here: the header is a static label (no switcher) that
          // wears the agent's own mark, the panel's logo when it has none
          // (#10910, UX retest of #10911, F2).
          logoIcon={AgentMark ? <AgentMark size={20} aria-hidden="true" /> : undefined}
          readOnly
          canRate={false}
          waitingGameHidden
          hideConversationList
        />
      </div>
    </div>
  );
}
