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
 * The shared `@filigran/chatbot` panel as XTM One hosts it, on every surface
 * that shows a conversation (/chat, the studio rails, an agent's
 * conversation): the web chat's routes, the session token and the sign-out
 * when it ends, the platform's language with its translations, its accent and
 * font, the cards of the answer markers, how a message reads, what XTM One
 * adds under a reply and in its footer, its tools' names, its working files,
 * the agent's page, the upload limit, and the waiting game with its gamepad in
 * the reasoning window. A surface passes what is its own
 * (the conversation, the agent, its slots) and may override any default.
 *
 * Imports the package root and its stylesheet: only a lazily loaded page may
 * import this module (`App.tsx`, `prefetch: false`).
 */
import "@filigran/chatbot/styles.css";
import { useCallback, useMemo, type RefObject } from "react";
import { useNavigate } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import {
  ChatPanel,
  type ChatAgentIconRenderer,
  type ChatMessage as PanelMessage,
  type ChatPanelHandle,
  type ChatPanelProps,
} from "@filigran/chatbot";
import { useArcadeWaitSignal } from "@/components/arcade/arcade-context";
import { ChatWaitingGame } from "@/components/chat/ChatWaitingGame";
import { ReasoningArcadeAction } from "@/components/chat/ReasoningArcadeAction";
import { MessageExtras, MessageFooterActions } from "@/components/chat-panel/MessageParts";
import { CHART_CODE_BLOCK_RENDERERS } from "@/components/chat-panel/chart-code-block";
import { CHAT_PANEL_ENDPOINTS } from "@/components/chat-panel/endpoints";
import {
  agentSwitchMarker,
  useAgentSwitchRenderer,
  useMarkerRenderers,
  useTranscriptCards,
} from "@/components/chat-panel/markers";
import { messageTextForPage } from "@/components/chat-panel/message-text";
import {
  PANEL_THEME,
  agentPageHref,
  isWorkingFile,
  panelLocale,
  useChatPanelHeaders,
  usePanelTranslate,
  usePlatformLook,
} from "@/components/chat-panel/panel-host";
import { messageIsRunReport } from "@/components/investigation/goal-state";
import {
  PresentationCardContext,
  type PresentationCardContextValue,
} from "@/components/presentation/presentation-card-context";
import { parsePresentationLink } from "@/components/presentation/slide-links";
import { useToast } from "@/components/ui/toast";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useFormatter } from "@/i18n";
import { API_BASE, api, endSession } from "@/lib/api";
import { ICON_MAP, MAX_CHAT_ATTACHMENTS_PER_MESSAGE, MAX_CHAT_UPLOAD_SIZE_BYTES, toolDisplayLabels } from "@/lib/constants";
import { stripBasePath } from "@/lib/base-path";
import { requestDesktopAttention } from "@/lib/desktop";
import { APP_LOCKED_EVENT, isAppLockedFrame } from "@/lib/app-lock";
import { namedSessionEndReason } from "@/lib/session";
import { cn } from "@/lib/utils";
import { answerParts, markerRefs } from "@/pages/chat/answerMarkers";
import { FileRevisionsContext } from "@/pages/chat/file-revisions-context";
import type { ChatMessage, Investigation } from "@/types";

/**
 * An agent's icon set by name (`Brain`, `Shield`), drawn as every other XTM
 * One screen draws it (`ICON_MAP`) in the conversation rows and the agent
 * switcher; a name the platform does not know keeps the panel's own glyph.
 */
const renderAgentIcon: ChatAgentIconRenderer = (name, { size }) => {
  const Icon = ICON_MAP[name];
  return Icon ? <Icon size={size} aria-hidden="true" /> : null;
};

/** What the host module sets, which a surface may still override. */
type HostDefaults =
  | "mode"
  | "apiBaseUrl"
  | "apiEndpoints"
  | "backendType"
  | "requestHeaders"
  | "user"
  | "locale"
  | "t"
  | "accentColor"
  | "fontFamily";

export interface XtmChatPanelProps
  extends Omit<ChatPanelProps, HostDefaults>,
    Partial<Pick<ChatPanelProps, HostDefaults>> {
  ref?: RefObject<ChatPanelHandle | null>;
  /**
   * The surface's own copy of the conversation on screen (`useConversation`):
   * the stored files the marker cards read, how a channel message reads, what
   * XTM One shows under a reply. A reply still streaming has none yet.
   */
  storedMessages?: ChatMessage[];
  /** Resumed replies under a cut turn's card, which says they resumed it (`useCutTurnCards`). */
  resumedShownAbove?: ReadonlySet<string>;
  /**
   * A turn runs on screen, as the panel said it (`onTurnRunningChange`): the
   * waiting game's wait source (`useArcadeWaitSignal`).
   */
  working?: boolean;
  /** No waiting game (a machine command on screen shows its own progress). */
  waitingGameHidden?: boolean;
  /** The user may rate the replies (not on a conversation shared read-only). */
  canRate?: boolean;
  /**
   * The studio rail's deck: its card collapses to a receipt, and a
   * slide link to it moves the stage beside the rail before any card's.
   */
  studioCards?: Pick<PresentationCardContextValue, "studioPresentationId" | "navigateToSlide">;
  /** Classes of the panel's container, which takes the platform's colour mode. */
  className?: string;
}

/** The investigations an answer's markers name, in order. */
const investigationIds = (text: string) => markerRefs(answerParts(text), "INVESTIGATION").map((ref) => ref.id);

/** The investigations whose report a stored reply holds as its text: the block shows it, the text does not. */
function useInvestigationReports(messages: ChatMessage[] | undefined, enabled: boolean): Map<string, string | null> {
  const ids = useMemo(() => {
    if (!enabled) return [];
    const found = new Set<string>();
    for (const m of messages ?? []) {
      if (m.role !== "assistant") continue;
      const runIds = investigationIds(m.content ?? "");
      if (runIds.length === 1) found.add(runIds[0]);
    }
    return [...found];
  }, [messages, enabled]);
  // Stable, so the map is rebuilt only when a run is read again.
  const combine = useCallback(
    (results: { data?: Investigation }[]) => {
      const reports = new Map<string, string | null>();
      ids.forEach((id, i) => {
        const run = results[i]?.data;
        if (run?.goal_plan) reports.set(id, run.report ?? null);
      });
      return reports;
    },
    [ids],
  );
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ["investigations", id],
      queryFn: () => api.get<Investigation>(`/investigations/${id}`),
      staleTime: 30_000,
    })),
    combine,
  });
}

export function XtmChatPanel({
  ref,
  storedMessages,
  resumedShownAbove,
  working = false,
  waitingGameHidden = false,
  canRate = true,
  studioCards,
  className,
  markerRenderers: extraMarkerRenderers,
  ...props
}: XtmChatPanelProps) {
  const { t_i18n, locale } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { data: me } = useMe();
  // The workspace groups the person opened, kept on their profile (#9179):
  // undefined until it is read, so the list takes them once it has them.
  const { mutate: saveProfile } = useUpdateProfile();
  const expandedWorkspaceGroups = me ? (me.chat_sidebar_expanded_workspaces ?? []) : undefined;
  const onExpandedWorkspaceGroupsChange = useCallback(
    (keys: string[]) => saveProfile({ chat_sidebar_expanded_workspaces: keys }),
    [saveProfile],
  );
  const requestHeaders = useChatPanelHeaders();
  const look = usePlatformLook();
  const t = usePanelTranslate();
  const deepInvestigation = useFeatureFlag("DEEP_INVESTIGATION");
  const videoStudio = useFeatureFlag("VIDEO_STUDIO");
  const conversationId = props.conversationId ?? null;

  const firstName = me?.full_name?.split(" ")[0] || "";
  const user = useMemo(() => ({ firstName }), [firstName]);
  const toolLabels = useMemo(() => toolDisplayLabels(t_i18n), [t_i18n]);

  /* ---------- The answer's markers and the cards around them ---------- */
  const dashboardStudio = useFeatureFlag("DASHBOARD_STUDIO");
  const builtInRenderers = useMarkerRenderers(storedMessages, { deepInvestigation, videoStudio, dashboardStudio });
  const agentSwitchRenderer = useAgentSwitchRenderer(storedMessages);
  const markerRenderers = useMemo(
    () => ({ ...builtInRenderers, ...agentSwitchRenderer, ...extraMarkerRenderers }),
    [builtInRenderers, agentSwitchRenderer, extraMarkerRenderers],
  );
  const transcriptCards = useTranscriptCards(conversationId, storedMessages);

  /* ---------- How a message reads, and what XTM One adds to it ---------- */
  const storedById = useMemo(() => new Map((storedMessages ?? []).map((m) => [m.id, m] as const)), [storedMessages]);
  const investigationReports = useInvestigationReports(storedMessages, deepInvestigation);
  const transformMessageText = useCallback(
    (text: string, message: PanelMessage) => {
      const stored = message.serverId ? storedById.get(message.serverId) : undefined;
      const switchMarker = agentSwitchMarker(stored);
      if (switchMarker) return switchMarker;
      if (message.role === "assistant") {
        // A reply that IS its investigation's report shows the block alone.
        const runIds = investigationIds(text);
        const report = runIds.length === 1 ? investigationReports.get(runIds[0]) : undefined;
        if (report !== undefined && messageIsRunReport(text, report)) return `[[INVESTIGATION:${runIds[0]}]]`;
      }
      const hasFiles = (message.files?.length ?? 0) > 0 || (message.attachments?.length ?? 0) > 0;
      return messageTextForPage(text, message.role, hasFiles, stored);
    },
    [storedById, investigationReports],
  );
  const renderMessageExtras = useCallback(
    (message: PanelMessage) => {
      const stored = message.serverId ? storedById.get(message.serverId) : undefined;
      // The card above says this reply resumed the cut turn.
      const shown = stored && resumedShownAbove?.has(stored.id) ? { ...stored, resumed_from: null } : stored;
      return <MessageExtras message={message} stored={shown} />;
    },
    [storedById, resumedShownAbove],
  );
  const messageFooterActions = useCallback(
    (message: PanelMessage) => (
      <MessageFooterActions
        message={message}
        stored={message.serverId ? storedById.get(message.serverId) : undefined}
        conversationId={conversationId}
        isAdmin={isAdmin}
        canRate={canRate}
      />
    ),
    [storedById, conversationId, isAdmin, canRate],
  );

  /* ---------- Waiting, links, downloads, a turn ending out of sight ---------- */
  const arcadeWaitSource = useArcadeWaitSignal(conversationId, working);
  const renderWaitingPanel = useCallback(() => <ChatWaitingGame waitSource={arcadeWaitSource} />, [arcadeWaitSource]);
  // The reasoning window holds the waiting panel's spot on most turns: the
  // game is offered in its corner too, and hidden with the waiting game.
  const renderReasoningAction = useCallback(
    () => (waitingGameHidden ? null : <ReasoningArcadeAction waitSource={arcadeWaitSource} />),
    [arcadeWaitSource, waitingGameHidden],
  );

  // A slide link to a deck on screen moves that deck: the studio's
  // stage first, then a card's; any other in-app link navigates.
  const studioNavigate = studioCards?.navigateToSlide;
  const studioPresentationId = studioCards?.studioPresentationId;
  const { navigateToSlide: cardsNavigate, cardContext: transcriptCardContext } = transcriptCards;
  const navigateToSlide = useCallback(
    (presentationId: string, slideRef: string) =>
      (studioNavigate?.(presentationId, slideRef) ?? false) || cardsNavigate(presentationId, slideRef),
    [studioNavigate, cardsNavigate],
  );
  const cardContext = useMemo(
    () =>
      studioPresentationId || studioNavigate
        ? { ...transcriptCardContext, studioPresentationId, navigateToSlide }
        : transcriptCardContext,
    [transcriptCardContext, studioPresentationId, studioNavigate, navigateToSlide],
  );
  const onRelativeLinkClick = useCallback(
    (href: string) => {
      const link = parsePresentationLink(href);
      if (link && link.slideRef !== null && navigateToSlide(link.presentationId, link.slideRef)) return;
      navigate(stripBasePath(href));
    },
    [navigate, navigateToSlide],
  );

  // An ended session (a `session_expired` frame, or a refusal saying so) signs
  // out and says why, as every other surface of the app does.
  const onSessionEnded = useCallback((reason: string | undefined) => endSession(namedSessionEndReason(reason)), []);

  // A stream that ends because the companion session locked says so: the lock
  // screen shows at once (`AppLockGate`), as on a 423, and the host still
  // hears every frame.
  const hostStreamEvent = props.onStreamEvent;
  const onStreamEvent = useCallback<NonNullable<ChatPanelProps["onStreamEvent"]>>(
    (frame, info) => {
      if (isAppLockedFrame(frame)) window.dispatchEvent(new CustomEvent(APP_LOCKED_EVENT, { detail: { code: "app_locked" } }));
      hostStreamEvent?.(frame, info);
    },
    [hostStreamEvent],
  );

  const onDownloadError = useCallback(() => toast({ title: t_i18n("Download failed"), variant: "error" }), [toast, t_i18n]);

  // The panel flashes the title and raises the browser notification when a
  // long turn ends while the user is away; inside XTM One Desktop the window
  // may sit behind another application, so it also asks for attention.
  const onTaskComplete = useCallback(
    (title: string, body: string) => {
      if (document.hidden || !document.hasFocus()) requestDesktopAttention();
      toast({ title, description: body, variant: "info" });
    },
    [toast],
  );

  return (
    // No colour-mode class here: the page's own on <html> themes the panel, and
    // the design system's `.dark` would declare its default tokens again over the
    // administrator's branding for everything inside.
    <div className={cn("flex-1 min-w-0", className)}>
      <PresentationCardContext.Provider value={cardContext}>
        <FileRevisionsContext.Provider value={transcriptCards.revisions}>
          <ChatPanel
            ref={ref}
            mode="inline"
            backendType="rest"
            apiBaseUrl={API_BASE}
            apiEndpoints={CHAT_PANEL_ENDPOINTS}
            requestHeaders={requestHeaders}
            user={user}
            expandedWorkspaceGroups={expandedWorkspaceGroups}
            onExpandedWorkspaceGroupsChange={onExpandedWorkspaceGroupsChange}
            locale={panelLocale(locale)}
            t={t}
            accentColor={look.accentColor}
            fontFamily={look.fontFamily}
            theme={PANEL_THEME}
            // The web chat reports the context occupancy on the iteration's
            // `thinking` status, on `done` and on a restored reply, the fields
            // the panel's own gauge reads.
            contextUsageEnabled
            // The upload route's own limit, on each file: the composer never
            // uploads a file the route would refuse. The route takes each
            // file alone, so a message's files have no total.
            maxFileSize={MAX_CHAT_UPLOAD_SIZE_BYTES}
            maxTotalSize={Number.POSITIVE_INFINITY}
            // A batch of screenshots, not the panel's default of 10.
            maxFileCount={MAX_CHAT_ATTACHMENTS_PER_MESSAGE}
            toolLabels={toolLabels}
            collapseUnnamedFiles={isWorkingFile}
            agentHref={agentPageHref}
            renderAgentIcon={renderAgentIcon}
            transformMessageText={transformMessageText}
            renderMessageExtras={renderMessageExtras}
            messageFooterActions={messageFooterActions}
            // Hidden, the waiting panel is gone, not merely emptied: an empty
            // render would show the package's own rotating messages.
            miniGameEnabled={!waitingGameHidden}
            renderWaitingPanel={renderWaitingPanel}
            renderReasoningAction={renderReasoningAction}
            codeBlockRenderers={CHART_CODE_BLOCK_RENDERERS}
            onRelativeLinkClick={onRelativeLinkClick}
            onDownloadError={onDownloadError}
            onTaskComplete={onTaskComplete}
            onSessionEnded={onSessionEnded}
            // The tip the old voice conversation showed: the call answers as fast
            // as the agent's model, so a quick model makes it feel live. The
            // panel shows it under the call header until closed for the session.
            voiceCallNotice={t_i18n("For faster voice responses, use a quick model like GPT-4o mini or Gemini Flash in your agent or platform settings.")}
            {...props}
            onStreamEvent={onStreamEvent}
            markerRenderers={markerRenderers}
          />
        </FileRevisionsContext.Provider>
      </PresentationCardContext.Provider>
    </div>
  );
}
