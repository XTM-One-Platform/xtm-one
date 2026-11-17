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

import { IconButton } from "@filigran/design-system";
import { memo, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Plus,
  MessageSquare,
  Bot,
  Trash2,
  ListX,
  Sparkles,
  ChevronDown,
  Check,
  Search,
  Info,
  Pencil,
  Eye,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Badge } from "@/components/ui/badge";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { ChannelIcon } from "@/components/ui/channel-icon";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { Tip } from "@/components/ui/tooltip";
import { ListSkeleton } from "@/components/ui/skeleton";
import { CHANNEL_TYPE_LABELS } from "@/lib/constants";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useMinuteTick } from "@/hooks/useMinuteTick";
import { cn } from "@/lib/utils";
import { conversationAgentId, generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import type { ConversationListItem, Agent, GeneralAssistantConfig } from "@/types";
import { groupConversations, groupIsOpen } from "@filigran/chatbot/workspaces";
import {
  MoveToWorkspaceMenu,
  UnfiledSection,
  WorkspaceGroupSection,
  WorkspacesHeading,
} from "@/pages/chat/WorkspaceGroups";
import {
  conversationDragProps,
  useSidebarWorkspaces,
  type SidebarWorkspaces,
} from "@/pages/chat/useSidebarWorkspaces";
import { formatTimeAgo } from "@/lib/relative-time";

interface ConversationSidebarProps {
  conversations: ConversationListItem[] | undefined;
  /**
   * The first read of the list is still in flight: a list skeleton stands in
   * for it, never the "No conversations yet" empty state.
   */
  conversationsLoading?: boolean;
  /**
   * The read of the list failed and nothing was read before: one inline,
   * catalogued error line stands in for it, never the empty state nor the
   * raw backend detail.
   */
  conversationsFailed?: boolean;
  activeConvId: string | null;
  selectedAgent: string | null;
  agents: Agent[] | undefined;
  showSidebar: boolean;
  onSelectConversation: (id: string) => void;
  /** Start a conversation, filed into *workspaceId* when given. */
  onNewConversation: (workspaceId?: string) => void;
  onArchive: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onClearAll: () => Promise<void>;
  onSelectAgent: (agentId: string | null) => void;
  gaConfig?: GeneralAssistantConfig | null;
}

function ConversationSidebarView({
  conversations,
  conversationsLoading = false,
  conversationsFailed = false,
  activeConvId,
  selectedAgent,
  agents,
  showSidebar,
  onSelectConversation,
  onNewConversation,
  onArchive,
  onRename,
  onClearAll,
  onSelectAgent,
  gaConfig,
}: ConversationSidebarProps) {
  // CE-only / unlicensed XTM One: disable the "new conversation" button
  // so users can't kick off a fresh chat. The agent filter, conversation
  // list, rename and delete actions stay live so existing history remains
  // browsable. Matches the composer gating downstream (which disables the
  // textarea, send button and tool/voice/attach controls when
  // ``chatSendEnabled`` is false).
  const { t_i18n } = useFormatter();
  const chatSendEnabled = useChatSendEnabled();
  // Admin-off reads the shared AI-disabled reason, the licence wording only
  // without a licence, as the composer does.
  const chatOffLabel =
    useAiDisabledReason() ?? t_i18n("Chat is unavailable. Start a trial, contact sales, or ask an admin to activate a license");
  const [convSearch, setConvSearch] = useState("");
  const [agentDropdownOpen, setAgentDropdownOpen] = useState(false);
  const [agentSearch, setAgentSearch] = useState("");
  const agentDropdownRef = useRef<HTMLDivElement>(null);
  const [ConfirmEl, confirm] = useConfirm();
  // Workspace groups: off - and the list flat, as it always was -
  // without Enterprise Edition or until the workspaces answered.
  const workspaceSidebar = useSidebarWorkspaces();

  // Inline rename state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId && renameInputRef.current) {
      renameInputRef.current.focus();
      renameInputRef.current.select();
    }
  }, [editingId]);

  const startRename = (conv: ConversationListItem) => {
    setEditingId(conv.id);
    setEditingTitle(conv.title || "");
  };

  const commitRename = () => {
    const id = editingId;
    if (!id) return; // already committed or cancelled
    const trimmed = editingTitle.trim();
    setEditingId(null);
    setEditingTitle("");
    if (trimmed) {
      onRename(id, trimmed);
    }
  };

  const cancelRename = () => {
    setEditingId(null);
    setEditingTitle("");
  };

  // Close dropdown on click outside
  useClickOutside(agentDropdownRef, () => setAgentDropdownOpen(false), agentDropdownOpen);

  const getAgentName = useCallback(
    (agentId: string | null) => {
      if (!agentId || !agents) return null;
      return agents.find((a) => a.id === agentId)?.name || null;
    },
    [agents],
  );

  const matchesSearch = (conv: ConversationListItem) => {
    const q = convSearch.toLowerCase();
    return !!(
      conv.title?.toLowerCase().includes(q) ||
      conv.last_message_preview?.toLowerCase().includes(q) ||
      getAgentName(conversationAgentId(conv))?.toLowerCase().includes(q)
    );
  };

  // Agent combobox: filtered + sorted lists
  const agentSearchLower = agentSearch.toLowerCase();
  const chatAgentsSorted = (agents ?? [])
    .filter((a) => a.enabled && !a.disable_chat && a.name.toLowerCase().includes(agentSearchLower))
    .sort((a, b) => a.name.localeCompare(b.name));
  const assignmentAgentsSorted = (agents ?? [])
    .filter((a) => a.enabled && a.disable_chat && a.name.toLowerCase().includes(agentSearchLower))
    .sort((a, b) => a.name.localeCompare(b.name));
  const generalMatch = "general assistant".includes(agentSearchLower);
  const linkedAgentId = generalAssistantStandIn(gaConfig?.linked_agent_id, agents)?.id ?? null;
  // The agent standing in for the General Assistant replaces its entry
  const showGA = !linkedAgentId && generalMatch;

  return (
    <div
      className={cn(
        "flex flex-col border-r border-elevation-subtle-soft layer-1 bg-elevation-default transition-all duration-300 shrink-0 overflow-hidden",
        showSidebar ? "w-72" : "w-0",
      )}
    >
      {ConfirmEl}
      {workspaceSidebar.filingConfirm}
      {/* Header */}
      <div className="flex h-[60px] items-center justify-between border-b border-elevation-subtle-soft px-4">
        <h2 className="content-base font-semibold">{t_i18n("Conversations")}</h2>
        <div className="flex items-center gap-0.5">
          {conversations && conversations.length > 0 && (
            <Tip content={t_i18n("Clear all conversations")}>
              <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Clear all conversations")} icon={<ListX className="h-3.5 w-3.5" />} className="h-7 w-7 text-default-secondary hover:text-feedback-error-primary" onClick={async () => { const ok = await confirm({ title: "Clear all conversations", description: "This will archive all your conversations. This action cannot be undone.", confirmLabel: "Clear all", variant: "danger", }); if (ok) await onClearAll(); }} />
            </Tip>
          )}
          {/* wrap="auto": while chat is off the disabled Button sits in a
              focusable span, so the reason opens on hover and on keyboard
              focus; Radix tooltips don't fire on disabled triggers. */}
          <Tip
            content={chatSendEnabled ? t_i18n("New conversation") : chatOffLabel}
            wrap="auto"
          >
            <IconButton variant="default" priority="tertiary" aria-label={chatSendEnabled ? t_i18n("New conversation") : chatOffLabel} icon={<Plus className="h-4 w-4" />} className="h-7 w-7" onClick={() => onNewConversation()} disabled={!chatSendEnabled} />
          </Tip>
        </div>
      </div>

      {/* Agent filter + search */}
      <div className="px-3 py-2 space-y-2">
        {/* Agent searchable combobox */}
        <div ref={agentDropdownRef} className="relative">
          <button
            type="button"
            onClick={() => { setAgentDropdownOpen((o) => !o); setAgentSearch(""); }}
            className={cn(
              "flex h-8 w-full items-center justify-between gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-3 content-compact",
              "ring-offset-focus focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
              "hover:bg-elevation-hover transition-colors",
            )}
          >
            <span className="truncate">
              {selectedAgent ? (getAgentName(selectedAgent) || t_i18n("Agent")) : t_i18n("General Assistant")}
            </span>
            <ChevronDown className={cn("h-3.5 w-3.5 shrink-0 opacity-50 transition-transform", agentDropdownOpen && "rotate-180")} />
          </button>

          {agentDropdownOpen && (
            <div className="absolute left-0 right-0 top-[calc(100%+4px)] z-50 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default text-default-primary shadow-overlay animate-in fade-in-0 zoom-in-95 slide-in-from-top-2">
              {/* Search input */}
              <div className="p-1.5 border-b border-elevation-subtle-soft">
                <SearchInput
                  autoFocus
                  placeholder={t_i18n("Search agents...")}
                  value={agentSearch}
                  onChange={setAgentSearch}
                  onKeyDown={(e) => { if (e.key === "Escape") setAgentDropdownOpen(false); }}
                />
              </div>

              {/* Agent list */}
              <div className="max-h-[240px] overflow-y-auto p-1">
                {/* General Assistant */}
                {showGA && (
                  <button
                    type="button"
                    className={cn(
                      "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 content-compact text-left transition-colors",
                      !selectedAgent ? "bg-elevation-hover text-default-primary" : "hover:bg-elevation-hover",
                    )}
                    onClick={() => { onSelectAgent(null); setAgentDropdownOpen(false); }}
                  >
                    <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" />
                    <span className="truncate">{t_i18n("General Assistant")}</span>
                    {!selectedAgent && <Check className="h-3 w-3 ml-auto text-accent shrink-0" />}
                  </button>
                )}

                {/* Chat-enabled agents (alphabetical) */}
                {chatAgentsSorted.map((a) => (
                  <button
                    type="button"
                    key={a.id}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 content-compact text-left transition-colors",
                      selectedAgent === a.id ? "bg-elevation-hover text-default-primary" : "hover:bg-elevation-hover",
                    )}
                    onClick={() => { onSelectAgent(a.id); setAgentDropdownOpen(false); }}
                  >
                    <Bot className="h-3.5 w-3.5 text-accent shrink-0" />
                    <span className="truncate">{a.name}</span>
                    {linkedAgentId === a.id && (
                      <Sparkles className="h-3 w-3 text-accent shrink-0" />
                    )}
                    {a.is_company_managed && <CompanyManagedBadge size="sm" label="Company" />}
                    {selectedAgent === a.id && <Check className="h-3 w-3 ml-auto text-accent shrink-0" />}
                  </button>
                ))}

                {/* Assignment-only agents (alphabetical, greyed out) */}
                {assignmentAgentsSorted.length > 0 && (
                  <>
                    <div className="px-2 py-1.5 mt-1 border-t border-elevation-subtle-soft">
                      <p className="text-[9px] text-default-secondary flex items-center gap-1">
                        <Info className="h-2.5 w-2.5" /> {t_i18n("Not recommended for chat")}
                      </p>
                    </div>
                    {assignmentAgentsSorted.map((a) => (
                      <Tip content={t_i18n("This agent is designed for assignments, not chat. You can still try if you know what you're doing.")}>
                        <button
                          type="button"
                          key={a.id}
                          className={cn(
                            "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 content-compact text-left transition-colors text-default-secondary",
                            selectedAgent === a.id ? "bg-elevation-hover" : "hover:bg-elevation-hover",
                          )}
                          onClick={() => { onSelectAgent(a.id); setAgentDropdownOpen(false); }}
                        >
                          <Bot className="h-3.5 w-3.5 shrink-0 opacity-50" />
                          <span className="truncate opacity-50">{a.name}</span>
                          {selectedAgent === a.id
                            ? <Check className="h-3 w-3 ml-auto text-accent shrink-0" />
                            : <Info className="h-3 w-3 ml-auto text-default-disabled shrink-0" />}
                        </button>
                      </Tip>
                    ))}
                  </>
                )}

                {/* Empty state */}
                {!showGA && chatAgentsSorted.length === 0 && assignmentAgentsSorted.length === 0 && (
                  <div className="py-3 text-center">
                    <p className="content-compact text-default-secondary">{t_i18n("No agents found")}</p>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-default-secondary" />
          <Input aria-label={t_i18n("Search conversations...")}
            placeholder={t_i18n("Search conversations...")}
            value={convSearch}
            onChange={(e) => setConvSearch(e.target.value)}
            className="h-8 pl-8 content-compact"
          />
        </div>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto">
        {/* Grouped by workspace, the headers stay mounted while the list loads
            and the skeleton sits under NOT IN A WORKSPACE. */}
        {conversationsLoading && !conversations && !workspaceSidebar.enabled ? (
          <ListSkeleton
            label={t_i18n("Loading chats...")}
            data-testid="conversation-list-loading"
            className="p-2"
          />
        ) : conversationsFailed && !conversations ? (
          <p
            role="alert"
            data-testid="conversation-list-error"
            className="px-4 py-6 text-center content-compact text-default-secondary"
          >
            {t_i18n("Could not load your conversations.")}
          </p>
        ) : workspaceSidebar.enabled ? (
          <GroupedConversations
            sidebar={workspaceSidebar}
            conversations={conversations ?? []}
            loading={!!conversationsLoading && !conversations}
            matches={convSearch ? matchesSearch : undefined}
            activeConvId={activeConvId}
            canCreateConversation={chatSendEnabled}
            onNewConversation={onNewConversation}
            renderRow={(conv) => (
              <ConversationRow
                key={conv.id}
                conv={conv}
                agentName={getAgentName(conversationAgentId(conv))}
                isActive={activeConvId === conv.id}
                isEditing={editingId === conv.id}
                editingTitle={editingTitle}
                renameInputRef={renameInputRef}
                onSelect={onSelectConversation}
                onStartRename={startRename}
                onEditingTitleChange={setEditingTitle}
                onCommitRename={commitRename}
                onCancelRename={cancelRename}
                onArchive={onArchive}
                workspaces={workspaceSidebar}
                t_i18n={t_i18n}
              />
            )}
          />
        ) : conversations && conversations.length > 0 ? (
          <div className="p-2 space-y-0.5">
            {conversations.filter((conv) => !convSearch || matchesSearch(conv)).map((conv) => (
              <ConversationRow
                key={conv.id}
                conv={conv}
                agentName={getAgentName(conversationAgentId(conv))}
                isActive={activeConvId === conv.id}
                isEditing={editingId === conv.id}
                editingTitle={editingTitle}
                renameInputRef={renameInputRef}
                onSelect={onSelectConversation}
                onStartRename={startRename}
                onEditingTitleChange={setEditingTitle}
                onCommitRename={commitRename}
                onCancelRename={cancelRename}
                onArchive={onArchive}
                t_i18n={t_i18n}
              />
            ))}
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center px-4 py-12">
            <MessageSquare className="h-8 w-8 text-default-disabled mb-3" />
            <p className="content-compact text-default-secondary">
              {t_i18n("No conversations yet")}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Memoized: the sidebar - a row and up to three tooltips per conversation -
 * re-renders only when its props change, as long as its handlers are stable.
 */
export const ConversationSidebar = memo(ConversationSidebarView);

/**
 * A row's relative time. The memoized sidebar re-renders only when its props
 * change, so this reads the shared minute tick to stay current on its own.
 */
function UpdatedAgo({ iso }: { iso: string }) {
  useMinuteTick();
  const { intl } = useFormatter();
  return <>{formatTimeAgo(intl, iso)}</>;
}

/**
 * The conversations grouped by workspace: the workspace groups, then
 * the conversations filed in none. Which groups exist, and which are open, is
 * the panel's own rule (`@filigran/chatbot/workspaces`).
 */
function GroupedConversations({
  sidebar,
  conversations,
  matches,
  activeConvId,
  canCreateConversation,
  onNewConversation,
  renderRow,
  loading = false,
}: {
  sidebar: SidebarWorkspaces;
  conversations: ConversationListItem[];
  /** The conversations' first read is in flight: the groups keep their
   *  headers, with no count, and the skeleton fills the unfiled section, where
   *  the list lands first. */
  loading?: boolean;
  matches?: (conv: ConversationListItem) => boolean;
  activeConvId: string | null;
  canCreateConversation: boolean;
  onNewConversation: (workspaceId?: string) => void;
  renderRow: (conv: ConversationListItem) => ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { groups, unfiled } = groupConversations(
    conversations.map((c) => ({ ...c, conversationId: c.id, workspaceId: c.workspace_id })),
    sidebar.workspaces,
    { matches },
  );
  const searching = !!matches;
  // Every group starts collapsed (#9179): the ones the person has not opened.
  const collapsed = new Set(groups.map((g) => g.key).filter((key) => !sidebar.expanded.has(key)));
  return (
    <div className="p-2 space-y-0.5">
      {!searching && <WorkspacesHeading sidebar={sidebar} />}
      {groups.map((group) => {
        const open = groupIsOpen(group.key, collapsed, group, activeConvId, searching);
        return (
          <WorkspaceGroupSection
            key={group.key}
            sidebar={sidebar}
            workspace={group.workspace}
            groupKey={group.key}
            count={loading ? undefined : group.conversations.length}
            open={open}
            onNewConversation={(workspaceId) => onNewConversation(workspaceId)}
            canCreateConversation={canCreateConversation}
          >
            {loading ? null : group.conversations.length > 0 ? (
              group.conversations.map(renderRow)
            ) : (
              <p className="px-3 py-1.5 content-caption text-default-secondary">
                {t_i18n("Drop a conversation here, or start one with +.")}
              </p>
            )}
          </WorkspaceGroupSection>
        );
      })}
      {(loading || unfiled.length > 0 || !searching) && (
        <UnfiledSection
          sidebar={sidebar}
          count={loading ? undefined : unfiled.length}
          onNewConversation={() => onNewConversation()}
          canCreateConversation={canCreateConversation}
        >
          {loading ? (
            <ListSkeleton label={t_i18n("Loading chats...")} data-testid="conversation-list-loading" />
          ) : unfiled.length > 0 ? (
            unfiled.map(renderRow)
          ) : (
            <p className="px-3 py-1.5 content-caption text-default-secondary">
              {conversations.length === 0
                ? t_i18n("No conversations yet")
                : t_i18n("Every conversation is in a workspace.")}
            </p>
          )}
        </UnfiledSection>
      )}
      {!loading && searching && groups.length === 0 && unfiled.length === 0 && (
        <p className="px-3 py-4 text-center content-compact text-default-secondary">{t_i18n("No conversation matches")}</p>
      )}
    </div>
  );
}

interface ConversationRowProps {
  conv: ConversationListItem;
  agentName: string | null | undefined;
  isActive: boolean;
  /**
   * Someone else's conversation, shared with the current user. Drops the
   * rename / delete controls (the endpoints are owner-only) and shows who it
   * came from instead.
   */
  readOnly?: boolean;
  isEditing?: boolean;
  editingTitle?: string;
  renameInputRef?: React.RefObject<HTMLInputElement | null>;
  onSelect: (id: string) => void;
  onStartRename?: (conv: ConversationListItem) => void;
  onEditingTitleChange?: (title: string) => void;
  onCommitRename?: () => void;
  onCancelRename?: () => void;
  onArchive?: (id: string) => void;
  /** The sidebar's workspaces: makes the row draggable onto a workspace group
   *  and offers "Move to workspace". */
  workspaces?: SidebarWorkspaces;
  t_i18n: (id: string, values?: Record<string, string | number>) => string;
}

/**
 * One row of the conversation sidebar.
 *
 * Extracted so the "Shared with me" section reuses the exact same row rather
 * than a near-copy: two implementations of this markup would drift, and the
 * one that drifts is always the one nobody looks at.
 */
export function ConversationRow({
  conv,
  agentName,
  isActive,
  readOnly = false,
  isEditing = false,
  editingTitle = "",
  renameInputRef,
  onSelect,
  onStartRename,
  onEditingTitleChange,
  onCommitRename,
  onCancelRename,
  onArchive,
  workspaces,
  t_i18n,
}: ConversationRowProps) {
  const movable = !readOnly && !isEditing && !!workspaces;
  return (
    <div
      role="button"
      tabIndex={0}
      {...conversationDragProps(conv.id, movable)}
      onClick={() => onSelect(conv.id)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onSelect(conv.id); }}
      className={cn(
        "group flex w-full items-start gap-2.5 rounded-sm px-3 py-2.5 text-left transition-all duration-200 cursor-pointer",
        isActive
          ? "bg-accent/10 text-default-primary"
          : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
      )}
    >
      <div
        className={cn(
          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-sm transition-colors",
          isActive ? "bg-accent/20" : "bg-elevation-hover",
        )}
      >
        {conversationAgentId(conv) ? (
          <Bot className="h-3.5 w-3.5" />
        ) : (
          <MessageSquare className="h-3.5 w-3.5" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        {isEditing ? (
          <form
            onSubmit={(e) => { e.preventDefault(); onCommitRename?.(); }}
            className="flex items-center gap-1"
            onClick={(e) => e.stopPropagation()}
          >
            <Input
              ref={renameInputRef}
              value={editingTitle}
              onChange={(e) => onEditingTitleChange?.(e.target.value)}
              onBlur={onCommitRename}
              onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onCancelRename?.(); } }}
              className="h-6 content-compact px-1.5"
              maxLength={500}
              aria-label="Rename conversation"
            />
          </form>
        ) : (
          <p className="truncate content-compact font-medium">
            {conv.title}
          </p>
        )}
        {conv.last_message_preview && (
          <p className="mt-0.5 truncate content-caption text-default-secondary">
            {conv.last_message_preview}
          </p>
        )}
        <div className="mt-1 flex items-center gap-1.5 min-w-0">
          {readOnly && conv.owner_name && (
            <Tip content={t_i18n("Shared by {name}", { name: conv.owner_name })}>
              <Badge
                variant="outline"
                className="max-w-[120px] min-w-0 overflow-hidden text-[9px] px-1.5 py-0 h-4 border-feedback-alert-primary/25 text-feedback-warning-primary"
              >
                <Eye className="mr-1 h-2.5 w-2.5 shrink-0" />
                <span className="truncate">{conv.owner_name}</span>
              </Badge>
            </Tip>
          )}
          {agentName && (
            <Tip content={agentName}>
              <Badge
                variant="outline"
                className="max-w-[120px] min-w-0 overflow-hidden text-[9px] px-1.5 py-0 h-4 border-accent/20 text-accent/70"
              >
                <span className="truncate">{agentName}</span>
              </Badge>
            </Tip>
          )}
          {/* A Slack / Teams direct-message thread continued on the web. */}
          {conv.channel && conv.channel !== "web" && (
            <Tip content={t_i18n("Started in {channel}", { channel: CHANNEL_TYPE_LABELS[conv.channel] ?? conv.channel })}>
              <span className="inline-flex shrink-0">
                <ChannelIcon channel={conv.channel} className="h-3 w-3" />
              </span>
            </Tip>
          )}
          <span className="shrink-0 text-[9px] text-default-secondary">
            <UpdatedAgo iso={conv.updated_at} />
          </span>
        </div>
      </div>
      {!readOnly && !isEditing && (
        <div className="flex items-center gap-0.5 shrink-0 self-center opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
          {movable && workspaces && (
            <MoveToWorkspaceMenu
              sidebar={workspaces}
              conversationId={conv.id}
              currentWorkspaceId={conv.workspace_id ?? null}
            />
          )}
          <Tip content={t_i18n("Rename")}>
            <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Rename")} icon={<Pencil className="h-3 w-3 text-default-secondary hover:text-default-primary" />} className="h-6 w-6" onClick={(e) => { e.stopPropagation(); onStartRename?.(conv); }} />
          </Tip>
          <Tip content={t_i18n("Delete")}>
            <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Delete")} icon={<Trash2 className="h-3 w-3 text-default-secondary hover:text-feedback-error-primary" />} className="h-6 w-6" onClick={(e) => { e.stopPropagation(); onArchive?.(conv.id); }} />
          </Tip>
        </div>
      )}
    </div>
  );
}
