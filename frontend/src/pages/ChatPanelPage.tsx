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
 * The /chat page on the shared `@filigran/chatbot` panel, over the web chat's
 * HTTP + SSE routes (`/api/v1/web`). The panel is the chat: the conversation
 * list, the thread, the composer, steering, approvals, Stop, a reload or a
 * second tab joining a turn. What is XTM One's own is the page's content in
 * the panel's slots: the session tools, Deep Investigation, the quota, the
 * machine a chat acts on, the conversation's banners and menu, and the cards
 * of its own answer markers.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigationType, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
// The package root, for its voice call check: this page is lazy and never
// prefetched (`App.tsx`), like `XtmChatPanel` that it mounts.
import {
  voiceCallSupported,
  type ChatConversationChange,
  type ChatConversationSummary,
  type ChatPanelHandle,
  type ChatSendDraft,
  type ChatSidebarSearch,
  type ChatWorkspace,
  type XtmAgent,
} from "@filigran/chatbot";
import { ChatActionsContext, type ChatActions } from "@/components/chat/chat-actions";
import { FileConversationDialog } from "@/components/chat/FileConversationDialog";
import { useConfirmSharedFiling } from "@/components/chat/useConfirmSharedFiling";
import { ChatFrame } from "@/components/chat-panel/ChatFrame";
import { XtmChatPanel } from "@/components/chat-panel/XtmChatPanel";
import { useHostSender } from "@/components/chat-panel/host-send";
import { transcriptFooter } from "@/components/chat-panel/transcript-footer";
import { FLOW_ENTRY_PREFIX, GENERAL_ASSISTANT_ENTRY } from "@/components/chat-panel/endpoints";
import { InvestigationBlock } from "@/components/investigation/InvestigationBlock";
import { SourcePackPicker } from "@/components/investigation/SourcePackPicker";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { WorkspaceKnowledgeDrawer } from "@/components/workspaces/WorkspaceKnowledgeDrawer";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useConversation,
  useConversations,
  useDismissInterruptedTurn,
  useExportConversation,
} from "@/hooks/api/useChatApi";
import { useFlowChatEntries } from "@/hooks/api/useFlowApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useDesktopLocalExecution } from "@/hooks/useDesktop";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useAgenticAIEnabled, useChatSendEnabled, useIsXtmOne, usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useChatLaunch } from "@/pages/chat-panel/useChatLaunch";
import { setChatActivity } from "@/lib/companion-chat-activity";
import {
  bumpConversation,
  lastConversationStorageKey,
  resolveResume,
  resumesOnArrival,
  resumesOnNavigation,
} from "@/lib/chat-navigation";
import { conversationAgentId } from "@/lib/general-assistant-stand-in";
import { isLiveRunnerActivity, renderedCommandIds } from "@/lib/runner-activity";
import { initialChatAgent, saveBlobAs } from "@/lib/utils";
import { ConversationFilesPanel } from "@/pages/chat/ConversationFilesPanel";
import { ContinueInChannelDialog } from "@/pages/chat/ContinueInChannelDialog";
import { ContinueWithAgentDialog } from "@/components/chat/ContinueWithAgentDialog";
import { RunnerActivityBlock } from "@/pages/chat/RunnerActivity";
import { ShareConversationDialog } from "@/pages/chat/ShareConversationDialog";
import { ComposerHeader, ComposerToolbar, ComposerVoiceCallButton } from "@/pages/chat-panel/ComposerParts";
import { composerPlaceholder, deepInvestigationTakes, filingTarget, placeholderAgentName } from "@/pages/chat-panel/composer-rules";
import { conversationMenuItems } from "@/pages/chat-panel/conversationMenu";
import { ConversationBanner, SharedConversationsSection } from "@/pages/chat-panel/ConversationBanner";
import { withConversation } from "@/pages/chat-panel/conversation-url";
import { createdFromDraft } from "@/pages/chat-panel/created-from-draft";
import { keepsInvestigationMode } from "@/pages/chat-panel/investigation-launch";
import { useSessionTools } from "@/pages/chat-panel/useSessionTools";
import { usePrimedConversationDraft } from "@/pages/chat-panel/usePrimedConversationDraft";
import { useTurnFrames } from "@/pages/chat-panel/useTurnFrames";
import { useCutTurnCards } from "@/components/chat-panel/cut-turn-cards";
import { useCompanionDevice } from "@/hooks/useCompanionDevice";
import { takeHandedFiles } from "@/lib/shared-inbox";
import type { ConversationListItem, SourcePackOptions } from "@/types";

/** Narrower than this, the conversation list starts collapsed. */
const NARROW_CHAT_QUERY = "(max-width: 1023px)";

/** Namespaces the panel's own browser storage (drafts, list width, folded workspaces). */
const STORAGE_PREFIX = "xtm-chat-page";

/** The longest investigation subject the source-pack routes take (`PackPreviewRequest.subject`). */
const INVESTIGATION_SUBJECT_MAX_CHARS = 16_000;


/** A Deep Investigation a link asks for: its subject, and the source pack it prefers. */
interface InvestigationLaunch {
  subject: string;
  pack: string | null;
}
export default function ChatPanelPage() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const conversationId = searchParams.get("c");
  const { data: user } = useMe();
  const { data: agents, dataUpdatedAt: agentsUpdatedAt } = useAgents();
  const { data: gaConfig, isLoading: gaConfigLoading } = useGeneralAssistantConfig();
  const isXtmOne = useIsXtmOne();
  const chatSendEnabled = useChatSendEnabled();
  const aiOff = useAiActionDisabled();
  const agenticLicensed = useAgenticAIEnabled();
  const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");
  const panelRef = useRef<ChatPanelHandle>(null);
  const hostSend = useHostSender(panelRef);

  // What a launcher handed this page (`chat-launch.ts`, the standalone home): read once, cleared after commit.
  const launch = useChatLaunch(!!conversationId);

  /* ---------- The agent a new chat starts with ---------- */
  // Resolved once, before the panel mounts: the panel selects it when its
  // agent list loads, and never asks again. `?agent=` first, then the
  // standalone home's hand-off, then the user's default.
  const [agentParam] = useState(() => searchParams.get("agent"));
  const [initialAgent, setInitialAgent] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    if (initialAgent !== undefined || !user || !agents || gaConfigLoading) return;
    const resolved = initialChatAgent(
      { agentParam, pendingAgent: launch.pendingAgent },
      user,
      agents,
      isXtmOne,
      gaConfig?.linked_agent_id ?? null,
    );
    // No agent is the General Assistant, which the picker lists as an entry of its own.
    setInitialAgent(resolved?.id ?? GENERAL_ASSISTANT_ENTRY);
  }, [initialAgent, user, agents, gaConfig, gaConfigLoading, agentParam, launch.pendingAgent, isXtmOne]);

  // The picker's entry, and the agent it stands for: none for the General
  // Assistant, the flow's agent for a chat-flow.
  const flowBuilderEnabled = useFeatureFlag("FLOW_BUILDER");
  const { data: flowEntries, dataUpdatedAt: flowsUpdatedAt } = useFlowChatEntries(flowBuilderEnabled);
  const [selectedEntry, setSelectedEntry] = useState<string | null>(null);
  useEffect(() => {
    if (initialAgent !== undefined) setSelectedEntry(initialAgent);
  }, [initialAgent]);
  const onAgentChange = useCallback((agent: XtmAgent) => setSelectedEntry(agent.id), []);
  const selectedAgentId = useMemo(() => {
    if (!selectedEntry || selectedEntry === GENERAL_ASSISTANT_ENTRY) return null;
    if (!selectedEntry.startsWith(FLOW_ENTRY_PREFIX)) return selectedEntry;
    const flowId = selectedEntry.slice(FLOW_ENTRY_PREFIX.length);
    return flowEntries?.find((f) => f.flow_id === flowId)?.agent_id ?? null;
  }, [selectedEntry, flowEntries]);

  /* ---------- The conversation on screen ---------- */
  // A turn in flight this page follows no stream of (come back to, or
  // reloaded, during it): the conversation is read again until it ends.
  const frames = useTurnFrames(conversationId);
  const conversationQuery = useConversation(conversationId ?? "", {
    notFoundIsOrdinary: true,
    followTurn: !frames.working,
  });
  const conversation = conversationId ? conversationQuery.data : undefined;
  const conversationAgent = conversation ? conversationAgentId(conversation) : null;
  const agentForNext = conversation ? conversationAgent : selectedAgentId;
  const agentForNextRef = useRef(agentForNext);
  agentForNextRef.current = agentForNext;
  const isReadOnlyConv = conversation?.access === "read";
  const isChannelOnlyConv = conversation?.web_continuable === false;

  // The conversation the new chat's draft created, while on screen: it keeps the
  // tools and the machine switch chosen before the first send (`createdFromDraft`).
  const createdFromDraftRef = useRef<string | null>(null);
  const createdHere = useCallback((id: string) => createdFromDraftRef.current === id, []);
  const desktopLocal = useDesktopLocalExecution(conversationId);
  const leavingConversationRef = useRef(false);
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  // react-router's functional update starts from the `searchParams` of the
  // render that made that `setSearchParams`, and the panel calls back before
  // the page re-renders after the launcher took `?agent=` off the URL: that
  // put the agent back and started a new chat over the conversation just
  // created (#10912). The browser's URL is the one on screen, already replaced.
  const setSearchParamsRef = useRef(setSearchParams);
  setSearchParamsRef.current = setSearchParams;
  const onConversationChange = useCallback(
    (id: string | null, change: ChatConversationChange) => {
      if (id === conversationIdRef.current) return;
      // Only one the panel created from a new chat, never one opened right after it,
      // nor one created earlier on the page and opened again.
      createdFromDraftRef.current = createdFromDraft(id, change);
      if (createdFromDraftRef.current) desktopLocal.carryOver(createdFromDraftRef.current);
      // The panel left the conversation (a new one, a deletion): the bare
      // `/chat` this writes stays a new conversation, not a resume.
      if (!id) leavingConversationRef.current = true;
      setSearchParamsRef.current(withConversation(new URLSearchParams(window.location.search), id));
    },
    [desktopLocal],
  );

  const tools = useSessionTools({ agentId: agentForNext, conversation, createdHere });
  const storedMessages = conversation?.messages;
  // The Concierge companion follows the turn on screen.
  useEffect(() => {
    setChatActivity(frames.working ? "running" : "idle");
    return () => setChatActivity("idle");
  }, [frames.working]);

  const desktopRunnerId = desktopLocal.desktopRunnerId;
  const getRequestExtras = useCallback(
    () => ({
      ...tools.requestFields(agentForNextRef.current),
      ...(desktopRunnerId ? { desktop_runner_id: desktopRunnerId } : {}),
    }),
    [tools, desktopRunnerId],
  );

  /* ---------- What a block or a notice sends ---------- */
  // Straight to the conversation as its next message, never through the
  // composer, with the tools the work needs added to this turn only.
  const sendForTheUser = useCallback(
    (prompt: string, options?: { builtinToolIds?: string[]; integrationIds?: string[] }) => {
      const text = prompt.trim();
      if (!text || !conversationIdRef.current) return;
      const fields = tools.requestFields(agentForNextRef.current);
      // Not resolved yet: `canSend` waits for it, a turn-only list would strip the agent's tools.
      if (!fields.builtin_tool_ids || !fields.integration_ids) return;
      const withExtra = (base: string[], extra: string[] = []) => [...base, ...extra.filter((id) => !base.includes(id))];
      void hostSend.send(text, {
        extras: {
          builtin_tool_ids: withExtra(fields.builtin_tool_ids, options?.builtinToolIds),
          integration_ids: withExtra(fields.integration_ids, options?.integrationIds),
          tool_selection_scope: "turn",
        },
      });
    },
    [tools, hostSend],
  );
  const readOnly = !chatSendEnabled || aiOff || isReadOnlyConv || isChannelOnlyConv;
  const chatActions = useMemo<ChatActions | null>(
    () =>
      isReadOnlyConv
        ? null
        : {
            canSend: !readOnly && !frames.working && tools.initialized && !!conversationId,
            sendPrompt: sendForTheUser,
            // A block says the tools are loading rather than staying silently disabled.
            toolsResolving: !!conversationId && !tools.initialized,
          },
    [isReadOnlyConv, readOnly, frames.working, tools.initialized, conversationId, sendForTheUser],
  );
  const dismissInterrupted = useDismissInterruptedTurn(conversationId);

  /* ---------- Deep Investigation ---------- */
  // With the mode on, the next message from the composer is a subject: the
  // send is taken over and the source-pack picker opens on it. The composer
  // keeps the text a taken-over send stands for until the send lands
  // (`host-send.ts`), a message held while a turn ran included.
  const [deepInvestigation, setDeepInvestigation] = useState(false);
  const [packPicker, setPackPicker] = useState<{ subject: string; typed: string } | null>(null);
  const [preferredPackSlugs, setPreferredPackSlugs] = useState<string[] | null>(null);
  // Off with each conversation change but the one a launch (`?investigate=`)
  // makes (`keepsInvestigationMode`); any change forgets the launch.
  const investigationLeavesRef = useRef<string | null>(null);
  const shownConversationRef = useRef(conversationId);
  useEffect(() => {
    const left = shownConversationRef.current;
    shownConversationRef.current = conversationId;
    const launchLeft = investigationLeavesRef.current;
    investigationLeavesRef.current = null;
    if (keepsInvestigationMode({ left, next: conversationId, launchLeft })) return;
    setDeepInvestigation(false);
  }, [conversationId]);
  const deepInvestigationRef = useRef(deepInvestigation);
  deepInvestigationRef.current = deepInvestigation;
  const queryClient = useQueryClient();
  const onBeforeSend = useCallback(
    (draft: ChatSendDraft) => {
      const investigation = deepInvestigationTakes(draft, deepInvestigationRef.current);
      // Files attached before the mode was turned on: refused with why, never
      // sent without them.
      if (investigation === "files") {
        toast({
          title: t_i18n("Deep Investigation takes a subject, not files"),
          description: t_i18n("Remove the attachments, or turn Deep Investigation off to send them."),
          variant: "info",
        });
        return false;
      }
      if (investigation === "subject") {
        setPackPicker({ subject: draft.text.slice(0, INVESTIGATION_SUBJECT_MAX_CHARS), typed: draft.text });
        return false;
      }
      // The conversation moves to the top of every cached list at once:
      // the server orders them by `updated_at`, read again only once
      // the turn ends.
      const id = conversationIdRef.current;
      if (id) {
        const at = new Date().toISOString();
        queryClient.setQueriesData<ConversationListItem[]>({ queryKey: ["conversations"] }, (list) =>
          bumpConversation(list, id, { at, preview: draft.text }),
        );
      }
      return undefined;
    },
    [queryClient, toast, t_i18n],
  );
  const startInvestigation = (slugs: string[], options: SourcePackOptions | null) => {
    const picked = packPicker;
    setPackPicker(null);
    setPreferredPackSlugs(null);
    setDeepInvestigation(false);
    if (!picked) return;
    const { subject, typed } = picked;
    void hostSend.send(
      subject,
      {
        extras: {
          mode: "deep_investigation",
          source_pack_slugs: slugs,
          ...(options && Object.keys(options).length > 0 ? { source_pack_options: options } : {}),
        },
      },
      { composer: typed, restore: typed },
    );
  };

  /* ---------- The composer's placeholder ---------- */
  // The agent a message goes to: an open conversation's own, else the one
  // picked; the General Assistant's says it can mention another agent or a
  // conversation. A subject while Deep Investigation is on.
  const platformName = usePlatformName();
  const placeholderConversationAgent = conversation ? conversationAgent : undefined;
  const placeholder = useCallback(
    (agent: XtmAgent | null) =>
      composerPlaceholder(
        placeholderAgentName(
          placeholderConversationAgent,
          agents,
          agent && agent.id !== GENERAL_ASSISTANT_ENTRY ? agent.name : null,
        ),
        { deepInvestigation, platformName },
        t_i18n,
      ),
    [placeholderConversationAgent, agents, deepInvestigation, platformName, t_i18n],
  );

  /* ---------- The conversation list ---------- */
  // Filing a conversation into a workspace other people see asks first.
  const [filingConfirm, confirmSharedFiling] = useConfirmSharedFiling();
  const { data: workspaces } = useWorkspaces({ enabled: agenticLicensed, throwOnError: false });
  const onBeforeMove = useCallback(
    (_conversation: ChatConversationSummary, workspace: ChatWorkspace | null) => {
      const target = filingTarget(workspace, workspaces);
      return target ? confirmSharedFiling(target, user?.id) : true;
    },
    [workspaces, confirmSharedFiling, user?.id],
  );
  // The conversations shared with the user follow the list's search.
  const selectShared = useCallback((id: string) => setSearchParams((current) => withConversation(current, id)), [setSearchParams]);
  const sidebarSections = useCallback(
    ({ query }: ChatSidebarSearch) => (
      <SharedConversationsSection query={query} activeConversationId={conversationId} onSelect={selectShared} />
    ),
    [conversationId, selectShared],
  );

  /* ---------- Launchers: `?agent=`, `?investigate=&pack=`, a primed draft ---------- */
  // Once the panel has its agent and its conversation (`onReady`).
  const [panelReady, setPanelReady] = useState(false);
  // The conversation list opens beside the thread only where both fit: on a
  // phone or a narrow window it starts collapsed, or it would squeeze the
  // thread to a column of single words.
  const isPhone = useCompanionDevice();
  const [listStartsCollapsed] = useState(() => isPhone || (window.matchMedia?.(NARROW_CHAT_QUERY).matches ?? false));
  const onReady = useCallback(() => setPanelReady(true), []);
  // The panel's picker follows the platform's own agent and flow lists: an
  // agent created, renamed or removed here shows there when they refresh.
  const pickerSourcesSeen = useRef<string | null>(null);
  useEffect(() => {
    if (!panelReady) return;
    const seen = `${agentsUpdatedAt}:${flowsUpdatedAt}`;
    if (pickerSourcesSeen.current !== null && pickerSourcesSeen.current !== seen) panelRef.current?.reloadAgents();
    pickerSourcesSeen.current = seen;
  }, [panelReady, agentsUpdatedAt, flowsUpdatedAt]);
  // And when the page is back in view, as the panel's conversation list is:
  // an agent made in another tab is offered on return.
  useEffect(() => {
    if (!panelReady) return;
    const onReturn = () => {
      if (document.visibilityState === "visible") panelRef.current?.reloadAgents();
    };
    document.addEventListener("visibilitychange", onReturn);
    return () => document.removeEventListener("visibilitychange", onReturn);
  }, [panelReady]);
  // The `?agent=` the page mounted with is the initial agent, read once: the
  // first launcher pass after `onReady` takes it off the URL and nothing more.
  const mountAgentPendingRef = useRef(agentParam !== null);
  const [pendingInvestigation, setPendingInvestigation] = useState<InvestigationLaunch | null>(null);
  const openInvestigation = useCallback(({ subject, pack }: InvestigationLaunch) => {
    panelRef.current?.setDraft(subject);
    setPreferredPackSlugs(pack ? [pack] : null);
    setDeepInvestigation(true);
  }, []);
  useEffect(() => {
    if (!panelReady) return;
    const mountPass = mountAgentPendingRef.current;
    mountAgentPendingRef.current = false;
    const agent = searchParams.get("agent");
    const subject = searchParams.get("investigate");
    if (!agent && !subject) return;
    const next = new URLSearchParams(searchParams);
    next.delete("agent");
    next.delete("investigate");
    next.delete("pack");
    setSearchParams(next, { replace: true });
    // A later one (the page already open) starts a new chat with it, the same agent included.
    const startsNewChat = !!agent && !(mountPass && agent === agentParam);
    if (startsNewChat) panelRef.current?.newConversation({ agent });
    if (subject && deepInvestigationEnabled) {
      const investigation = { subject: subject.slice(0, INVESTIGATION_SUBJECT_MAX_CHARS), pack: searchParams.get("pack") };
      if (!startsNewChat) {
        openInvestigation(investigation);
        return;
      }
      // The new chat clears the composer, and leaving the conversation turns the
      // mode off: both wait for the panel to have started it (below).
      investigationLeavesRef.current = conversationIdRef.current;
      setPendingInvestigation(investigation);
    }
  }, [panelReady, searchParams, setSearchParams, agentParam, deepInvestigationEnabled, openInvestigation]);
  // Applied in the commit the panel's queued new chat runs in: a child's effects
  // run before its parent's, so the subject lands after the panel's reset.
  useEffect(() => {
    if (!pendingInvestigation) return;
    setPendingInvestigation(null);
    openInvestigation(pendingInvestigation);
  }, [pendingInvestigation, openInvestigation]);

  // Files another app shared to this one (`/share`): into the tray, unsent.
  useEffect(() => {
    if (!panelReady) return;
    const shared = takeHandedFiles();
    if (shared.length > 0) panelRef.current?.attachFiles(shared);
  }, [panelReady]);

  const launchedRef = useRef(false);
  useEffect(() => {
    if (!panelReady || launchedRef.current || !launch.draft.trim()) return;
    launchedRef.current = true;
    if (launch.autoSend && chatSendEnabled && !aiOff) {
      void hostSend.send(launch.draft.trim());
      return;
    }
    panelRef.current?.setDraft(launch.draft);
    panelRef.current?.focusComposer();
  }, [panelReady, launch, chatSendEnabled, aiOff, hostSend]);
  usePrimedConversationDraft(panelReady, conversationId, panelRef);

  /* ---------- "Chat" resumes a conversation ---------- */
  // The bare `/chat` lands on the conversation the person last opened (else
  // their most recent) instead of a new one: at the mount, and on every later
  // later navigation to it (the "Chat" entry clicked from this page, also on
  // the bare `/chat` itself, #7730). Not when something asked for a new
  // conversation: `?new=1`, a `?agent=` or `?investigate=` launch, a
  // launcher's agent or draft, the panel leaving its conversation
  // (`leavingConversationRef`), or a draft already typed.
  const location = useLocation();
  const navigationType = useNavigationType();
  // A failed read leaves the page as it is, a new conversation, never the route error boundary.
  const { data: conversationList, isError: conversationListFailed } = useConversations(50, { throwOnError: false });
  const lastConversationKey = user?.id ? lastConversationStorageKey(user.id) : null;
  const resumeRef = useRef(resumesOnArrival(searchParams, launch));
  // While the decision is pending, and until the picked conversation is open,
  // the panel shows its own history skeleton (`historyPending`) instead of the
  // welcome screen, which would otherwise flash first: one skeleton, in the
  // reading column, from the first paint to the conversation.
  const [resumePending, setResumePending] = useState(resumeRef.current);
  const armResume = useCallback((armed: boolean) => {
    resumeRef.current = armed;
    setResumePending(armed);
  }, []);
  const seenLocationRef = useRef({ key: location.key, search: location.search });
  useLayoutEffect(() => {
    const seen = seenLocationRef.current;
    if (seen.key === location.key) return;
    seenLocationRef.current = { key: location.key, search: location.search };
    const leaving = leavingConversationRef.current;
    leavingConversationRef.current = false;
    armResume(
      resumesOnNavigation(searchParams, {
        type: navigationType,
        previousSearch: seen.search,
        search: location.search,
        leavingConversation: leaving,
      }),
    );
  }, [location.key, location.search, navigationType, searchParams, armResume]);
  useEffect(() => {
    if (!resumeRef.current || !panelReady) return;
    // The list could not be read: nothing to resume, the page stays empty.
    if (!conversationListFailed && !lastConversationKey) return;
    let lastOpened: string | null = null;
    if (lastConversationKey) {
      try {
        lastOpened = localStorage.getItem(lastConversationKey);
      } catch {
        // Storage unavailable: resume the most recent one.
      }
    }
    const outcome = resolveResume({
      conversations: conversationList,
      failed: conversationListFailed,
      lastOpenedId: lastOpened,
      activeConvId: conversationIdRef.current,
      typing: !!panelRef.current?.getDraft().trim(),
    });
    if (!outcome.settled) return;
    if (outcome.keepPlaceholder) resumeRef.current = false;
    else armResume(false);
    const id = outcome.open;
    if (!id) return;
    setSearchParams((current) => withConversation(current, id), { replace: true });
  }, [panelReady, conversationList, conversationListFailed, lastConversationKey, setSearchParams, location.key, armResume]);
  // `?new=1` has said its piece once read: dropped (a replace, which arms no
  // resume) so a reload resumes as usual.
  useEffect(() => {
    if (!searchParams.has("new")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  // The conversation opened, for the next "Chat".
  useEffect(() => {
    if (!conversationId || !lastConversationKey) return;
    try {
      localStorage.setItem(lastConversationKey, conversationId);
    } catch {
      // Storage unavailable: "Chat" then resumes the most recent one.
    }
  }, [conversationId, lastConversationKey]);

  /* ---------- Dialogs and the conversation menu ---------- */
  const [showFilesPanel, setShowFilesPanel] = useState(false);
  const [showWorkspaceKnowledge, setShowWorkspaceKnowledge] = useState(false);
  const [showShareDialog, setShowShareDialog] = useState(false);
  const [showContinueDialog, setShowContinueDialog] = useState(false);
  const [showContinueWithAgent, setShowContinueWithAgent] = useState(false);
  const [showFileDialog, setShowFileDialog] = useState(false);
  useEffect(() => {
    setShowFilesPanel(false);
    setShowWorkspaceKnowledge(false);
    setShowShareDialog(false);
    setShowContinueDialog(false);
    setShowContinueWithAgent(false);
    setShowFileDialog(false);
  }, [conversationId]);

  // A conversation as a document: its charts are written as tables.
  const { mutateAsync: exportMutate } = useExportConversation();
  const handleExport = useCallback(
    async (format: "markdown" | "pdf") => {
      if (!conversationId) return;
      try {
        const { blob, filename } = await exportMutate({ conversationId, format });
        saveBlobAs(blob, filename ?? (format === "pdf" ? "conversation.pdf" : "conversation.md"));
      } catch (err) {
        toast({
          title: t_i18n("Export failed"),
          description: err instanceof Error && err.message ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [conversationId, exportMutate, t_i18n, toast],
  );

  const isOrgReadable = conversation?.link_access === "org_read";
  const workspaceId = conversation?.workspace_id ?? null;
  const isWebConversation = conversation?.channel === "web";
  const menuItems = useMemo(
    () =>
      conversationMenuItems(
        {
          conversationId,
          workspaceId,
          isReadOnly: isReadOnlyConv,
          isChannelOnly: isChannelOnlyConv,
          isWebConversation,
          turnRunning: frames.working,
          isOrgReadable,
          agenticLicensed,
        },
        {
          showFiles: () => setShowFilesPanel(true),
          share: () => setShowShareDialog(true),
          continueInChannel: () => setShowContinueDialog(true),
          continueWithAgent: () => setShowContinueWithAgent(true),
          exportAs: (format) => void handleExport(format),
          fileInWorkspace: () => setShowFileDialog(true),
          showWorkspaceKnowledge: () => setShowWorkspaceKnowledge(true),
        },
        t_i18n,
      ),
    [
      conversationId,
      workspaceId,
      agenticLicensed,
      handleExport,
      isReadOnlyConv,
      isChannelOnlyConv,
      isWebConversation,
      frames.working,
      isOrgReadable,
      t_i18n,
    ],
  );

  const jumpToMessage = useCallback(
    (messageId: string) => {
      if (panelRef.current?.scrollToMessage(messageId)) {
        setShowFilesPanel(false);
        return;
      }
      toast({
        title: t_i18n("Message not loaded"),
        description: t_i18n("Load older messages to reach the message this file came from."),
        variant: "info",
      });
    },
    [toast, t_i18n],
  );

  /* ---------- The page's own content in the thread ---------- */
  // The machine commands of the turn in flight that no stored reply shows yet,
  // the investigation it runs, what a pause waits on, and a last turn
  // that stopped before its answer.
  const liveRunnerActivity = useMemo(() => {
    const rendered = renderedCommandIds(storedMessages ?? []);
    return frames.runnerActivity.filter((record) => !rendered.has(record.command_id));
  }, [frames.runnerActivity, storedMessages]);
  const canResumeHere = !isReadOnlyConv && !isChannelOnlyConv;
  const resumeHere = canResumeHere ? () => sendForTheUser(t_i18n("Continue where you left off.")) : undefined;
  const dismissHere = isReadOnlyConv ? undefined : () => dismissInterrupted.mutate();
  const reloadPanelTranscript = useCallback(() => panelRef.current?.reloadConversation(), []);
  const cutTurns = useCutTurnCards(storedMessages, {
    resumingTurn: frames.resumingTurn,
    interruptedTurn: conversation?.interrupted_turn,
    working: frames.working,
    processing: !!conversation?.is_processing,
    reloadTranscript: reloadPanelTranscript,
    onResume: resumeHere,
    onDismiss: dismissHere,
    resumeDisabled: !chatActions?.canSend,
  });
  const footer = transcriptFooter({
    blocks: [
      cutTurns.liveCard,
      liveRunnerActivity.length > 0 && <RunnerActivityBlock records={liveRunnerActivity} />,
      frames.activeInvestigationId && deepInvestigationEnabled && (
        <InvestigationBlock key={frames.activeInvestigationId} investigationId={frames.activeInvestigationId} />
      ),
    ],
    working: frames.working,
    // A transcript ending with a cut says it on the cut's card instead.
    resumingTurn: cutTurns.endsWithCut ? null : frames.resumingTurn,
    wakingTurn: frames.wakingTurn,
    waitItems: frames.waitItems,
    interruptedTurn: cutTurns.endsWithCut ? null : conversation?.interrupted_turn,
    onResume: resumeHere,
    onDismiss: dismissHere,
    resumeDisabled: !chatActions?.canSend,
  });

  // A command already shows its own live progress: no game on top of it.
  const commandInFlight = liveRunnerActivity.some(isLiveRunnerActivity);

  // The agent is resolved before the panel mounts (it reads `initialAgent` once,
  // when its list lands): until then the chat's frame stays empty, never a
  // page skeleton of another shape that the panel's own skeleton would replace.
  if (initialAgent === undefined) return <ChatFrame />;

  const disabledComposerControls = readOnly || frames.working;
  return (
    <ChatFrame>
      <ChatActionsContext.Provider value={chatActions}>
        <XtmChatPanel
          ref={panelRef}
          initialConversationListCollapsed={listStartsCollapsed}
          replaceMessage={cutTurns.replaceMessage}
          renderAboveMessage={cutTurns.renderAboveMessage}
          resumedShownAbove={cutTurns.resumedIds}
          storedMessages={storedMessages}
          working={frames.working}
          waitingGameHidden={commandInFlight}
          canRate={!isReadOnlyConv}
          storageKeyPrefix={STORAGE_PREFIX}
          conversationId={conversationId}
          onConversationChange={onConversationChange}
          initialAgent={initialAgent ?? undefined}
          onAgentChange={onAgentChange}
          readOnly={readOnly}
          readOnlyNotice={
            aiOff ? (
              <AiDisabledNotice />
            ) : !chatSendEnabled ? (
              t_i18n("Chat is unavailable. Start a free XTM One trial or contact sales to enable agentic chat…")
            ) : undefined
          }
          getRequestExtras={getRequestExtras}
          onStreamEvent={frames.onStreamEvent}
          onTurnRunningChange={frames.onTurnRunningChange}
          onReady={onReady}
          onBeforeSend={onBeforeSend}
          onHeldSendSettled={hostSend.onHeldSendSettled}
          headerActions={<OverflowMenu items={menuItems} label={t_i18n("Conversation options")} />}
          banner={
            <ConversationBanner
              conversation={conversation}
              agentId={agentForNext}
              working={frames.working}
              aiOff={aiOff}
              onReturned={() => panelRef.current?.reloadConversation()}
            />
          }
          composerToolbar={
            <ComposerToolbar
              disabled={disabledComposerControls}
              tools={tools.pickerProps}
              toolCount={tools.toolCount}
              deepInvestigation={deepInvestigation}
              onToggleDeepInvestigation={
                deepInvestigationEnabled ? () => setDeepInvestigation((on) => !on) : undefined
              }
            />
          }
          composerTrailingToolbar={
            voiceCallSupported() ? (
              <ComposerVoiceCallButton
                disabled={disabledComposerControls}
                onStart={() => panelRef.current?.startVoiceCall()}
              />
            ) : undefined
          }
          composerHeader={
            <ComposerHeader
              deepInvestigation={deepInvestigation && !readOnly}
              runnerApprovals={isReadOnlyConv ? [] : frames.runnerApprovals}
              onRunnerApprovalDecided={frames.dismissRunnerApproval}
              desktop={isReadOnlyConv ? null : desktopLocal}
            />
          }
          transcriptFooter={footer}
          historyPending={resumePending && !conversationId}
          sidebarSections={sidebarSections}
          onBeforeMove={onBeforeMove}
          // The backend's Clear all archives the conversations.
          clearAllAction="archive"
          placeholder={placeholder}
          // Deep Investigation takes a subject, never files.
          disableFileManagement={deepInvestigation}
          agentMentions
          steerAttachments
          holdSendsDuringTurn
        />
      </ChatActionsContext.Provider>

      {filingConfirm}
      <SourcePackPicker
        subject={packPicker?.subject ?? null}
        preferredSlugs={preferredPackSlugs}
        onConfirm={startInvestigation}
        onCancel={() => setPackPicker(null)}
      />
      <ConversationFilesPanel
        conversationId={conversationId}
        conversationTitle={conversation?.title ?? null}
        open={showFilesPanel}
        onClose={() => setShowFilesPanel(false)}
        workspaceId={workspaceId}
        onJumpToMessage={jumpToMessage}
      />
      <WorkspaceKnowledgeDrawer
        workspaceId={workspaceId}
        open={showWorkspaceKnowledge}
        onClose={() => setShowWorkspaceKnowledge(false)}
      />
      <ShareConversationDialog
        conversationId={conversationId}
        conversationTitle={conversation?.title ?? null}
        open={showShareDialog}
        onClose={() => setShowShareDialog(false)}
      />
      <ContinueInChannelDialog
        conversationId={conversationId}
        open={showContinueDialog}
        onClose={() => setShowContinueDialog(false)}
      />
      <ContinueWithAgentDialog
        conversationId={conversationId}
        currentAgentId={conversation?.agent_id ?? null}
        deletedAgentName={conversation?.agent_id ? null : (conversation?.deleted_agent_name ?? null)}
        open={showContinueWithAgent}
        onClose={() => setShowContinueWithAgent(false)}
        panel={panelRef}
      />
      <FileConversationDialog
        conversationId={conversationId}
        currentWorkspaceId={workspaceId}
        open={showFileDialog}
        onClose={() => setShowFileDialog(false)}
      />
    </ChatFrame>
  );
}
