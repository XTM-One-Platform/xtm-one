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

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  MessageSquare,
  MessagesSquare,
  User,
  ChevronRight,
  Trash2,
  ThumbsUp,
  ThumbsDown,
  Percent,
  Hash,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { ChannelIcon } from "@/components/ui/channel-icon";
import { useAgentChats, useClearAgentChats } from "@/hooks/api/useAgentApi";
import { useDebounce } from "@/hooks/useDebounce";
import { cn } from "@/lib/utils";
import { CHANNEL_TYPE_LABELS, CHANNEL_ICONS, CHANNEL_ROUTING_ICON_KEY } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Agent, AgentChatItem } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const PAGE_SIZE = 20;

/** Resolve channel badge colors from the shared CHANNEL_ICONS constant. */
function channelBadgeStyle(channel: string): { bg: string; text: string } {
  const iconKey = CHANNEL_ROUTING_ICON_KEY[channel];
  const entry = iconKey ? CHANNEL_ICONS[iconKey] : undefined;
  return entry ? { bg: entry.bg, text: entry.fg } : { bg: "bg-elevation-hover", text: "text-default-secondary" };
}

// ---------------------------------------------------------------------------
// Chats Tab
// ---------------------------------------------------------------------------
export function ChatsTab({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState("");
  const debouncedSearch = useDebounce(searchQuery, 300);
  const [page, setPage] = useState(0);
  const [channelFilter, setChannelFilter] = useState<string | undefined>();

  const { data, isLoading } = useAgentChats(agent.id, {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    search: debouncedSearch || undefined,
    channel: channelFilter,
  });

  const clearChats = useClearAgentChats(agent.id);
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const stats = data?.stats;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // Reset page when search/filter changes
  const handleSearch = (value: string) => {
    setSearchQuery(value);
    setPage(0);
  };

  const handleChannelFilter = (ch: string | undefined) => {
    setChannelFilter(ch);
    setPage(0);
  };

  const handleClearAll = async () => {
    if (
      await confirm({
        title: "Clear all chats?",
        description: t_i18n("This will permanently delete {count, plural, one {# conversation and its messages} other {all # conversations and their messages}} for this agent. This action cannot be undone.", { count: stats?.total_conversations ?? 0 }),
        confirmLabel: "Clear All",
      })
    ) {
      clearChats.mutate(undefined, {
        onSuccess: () => toast({ title: t_i18n("All chats cleared"), variant: "success" }),
      });
    }
  };

  return (
    <div className="space-y-4">
      {/* Stats — always visible */}
      {stats && stats.total_conversations > 0 && (
        <>
          {/* Feedback summary */}
          <div className="grid gap-3 sm:grid-cols-5">
            <StatCard
              icon={MessagesSquare}
              label="Total Conversations"
              value={stats.total_conversations}
              subtitle="Across all channels"
            />
            <StatCard
              icon={MessageSquare}
              label="Total Messages"
              value={stats.total_messages}
              subtitle="Across all conversations"
            />
            <StatCard
              icon={ThumbsUp}
              label="Positive Feedback"
              value={stats.positive_feedback}
              subtitle="Thumbs up"
            />
            <StatCard
              icon={ThumbsDown}
              label="Negative Feedback"
              value={stats.negative_feedback}
              subtitle="Thumbs down"
            />
            <StatCard
              icon={Percent}
              label="Positive Ratio"
              value={
                stats.positive_feedback_ratio !== null
                  ? `${stats.positive_feedback_ratio}%`
                  : "—"
              }
              subtitle="All feedback"
            />
          </div>

          {/* Per-channel breakdown */}
          {stats.channels.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-4">
              {stats.channels.map((ch) => {
                const label = CHANNEL_TYPE_LABELS[ch.channel] ?? ch.channel;
                return (
                  <StatCard
                    key={ch.channel}
                    icon={Hash}
                    label={label}
                    value={ch.count}
                    subtitle={t_i18n("{channel} conversations", { channel: t_i18n(label) })}
                  />
                );
              })}
            </div>
          )}
        </>
      )}

      <div className="space-y-4">
          {/* Search + channel filter + clear button */}
          <div className="flex items-center gap-3">
            <SearchInput
              placeholder={t_i18n("Search conversations...")}
              value={searchQuery}
              onChange={handleSearch}
            />

            {/* Channel filter pills */}
            <div className="flex items-center gap-1.5">
              <Button
                variant={channelFilter === undefined ? "default" : "ghost"}
                size="sm"
                className={cn("h-7 px-2.5 content-compact", channelFilter === undefined && "bg-accent text-negative-primary hover:bg-accent/90")}
                onClick={() => handleChannelFilter(undefined)}
              >
                {t_i18n("All")}
              </Button>
              {(stats?.channels ?? []).map((ch) => (
                <Button
                  key={ch.channel}
                  variant={channelFilter === ch.channel ? "default" : "ghost"}
                  size="sm"
                  className={cn(
                    "h-7 px-2.5 content-compact gap-1.5",
                    channelFilter === ch.channel && "bg-accent text-negative-primary hover:bg-accent/90",
                  )}
                  onClick={() => handleChannelFilter(ch.channel === channelFilter ? undefined : ch.channel)}
                >
                  <ChannelIcon channel={ch.channel} className={channelFilter === ch.channel ? "text-negative-primary" : undefined} />
                  {t_i18n(CHANNEL_TYPE_LABELS[ch.channel] ?? ch.channel)}
                  <span className="opacity-60">{ch.count}</span>
                </Button>
              ))}
            </div>

            {/* Spacer, then the total once, at the top (#11191). */}
            <div className="flex-1" />
            {total > 0 && <TotalChip total={total} />}

            {/* Clear all chats button */}
            {canManage && total > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="h-8 gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10 shrink-0"
                disabled={clearChats.isPending}
                onClick={handleClearAll} aria-busy={clearChats.isPending || undefined}>
                {clearChats.isPending && <Spinner size="sm" tone="inherit" />}{!clearChats.isPending && <Trash2 className="h-3.5 w-3.5" />}
                {t_i18n("Clear All")}
              </Button>
            )}
          </div>

          {/* Conversation list with top + bottom pagination */}
          {isLoading && !data ? (
            <p className="content-compact text-default-secondary py-4">{t_i18n("Loading chats...")}</p>
          ) : (
            <div className="space-y-2">
              {items.length > 0 ? (
                items.map((chat) => (
                  <ChatRow
                    key={chat.id}
                    chat={chat}
                    onClick={() => navigate(`/agents/${agent.id}/chats/${chat.id}`)}
                  />
                ))
              ) : debouncedSearch || channelFilter ? (
                <div className="py-8 text-center">
                  <p className="content-base text-default-secondary">{t_i18n("No conversations matching your search.")}</p>
                </div>
              ) : (
                <Card className="border-dashed">
                  <CardContent className="flex flex-col items-center justify-center py-12 text-center">
                    <MessagesSquare className="h-10 w-10 text-default-disabled mb-3" />
                    <p className="content-base font-medium mb-1">{t_i18n("No conversations yet")}</p>
                    <p className="content-compact text-default-secondary max-w-md">
                      {t_i18n("When users interact with this agent via web chat, Slack, Teams, WhatsApp, Telegram, Discord, or Google Chat, their conversations will appear here.")}
                    </p>
                  </CardContent>
                </Card>
              )}

              <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
            </div>
          )}
      </div>

      {ConfirmEl}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Chat Row — compact conversation card
// ---------------------------------------------------------------------------
function ChatRow({ chat, onClick }: { chat: AgentChatItem; onClick: () => void }) {
  const { t_i18n, intl } = useFormatter();
  const badgeStyle = channelBadgeStyle(chat.channel);

  return (
    <Card
      className="cursor-pointer hover:bg-elevation-hover transition-all group"
      onClick={onClick}
    >
      <CardContent className="p-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            {/* Channel icon */}
            <div className={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-sm", badgeStyle.bg)}>
              <ChannelIcon channel={chat.channel} />
            </div>

            {/* Title + meta */}
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="content-compact font-semibold truncate max-w-[300px]">{chat.title}</span>
                <Badge
                  variant="outline"
                  className={cn("text-[8px] gap-1", badgeStyle.text)}
                >
                  <ChannelIcon channel={chat.channel} className="h-2.5 w-2.5" />
                  {t_i18n(CHANNEL_TYPE_LABELS[chat.channel] ?? chat.channel)}
                </Badge>
              </div>
              <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                {chat.user_name && (
                  <span className="inline-flex items-center gap-1 content-caption text-default-secondary">
                    <User className="h-2.5 w-2.5" />
                    {chat.user_name}
                  </span>
                )}
                <span className="content-caption text-accent">
                  {t_i18n("{count, plural, one {# message} other {# messages}}", { count: chat.message_count })}
                </span>
                {chat.last_message_preview && (
                  <span className="text-[9px] text-default-secondary truncate max-w-[300px]">
                    {chat.last_message_preview.length > 80
                      ? `${chat.last_message_preview.slice(0, 80)}...`
                      : chat.last_message_preview}
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* Right: time */}
          <div className="flex items-center gap-2 shrink-0">
            <span className="content-caption text-default-secondary">{formatTimeAgo(intl, chat.updated_at)}</span>
            <ChevronRight className="h-3.5 w-3.5 text-default-disabled group-hover:text-default-secondary transition-colors" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
