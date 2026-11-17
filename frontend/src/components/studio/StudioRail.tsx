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
 * The studio rail: a chat bound to the work beside it - a deck in the
 * Presentation Studio, a video in the Video Studio editor.
 *
 * Editing through an assistant used to mean leaving the work - priming the
 * chat composer with its id and hoping the agent found the right one. The
 * studio keeps the two together: this rail is the shared chat panel
 * (`XtmChatPanel`, the one `/chat` renders, loaded with the rail:
 * `LazyXtmChatPanel`) whose conversations are BOUND to
 * the subject on the stage (`Conversation.presentation_id` /
 * `Conversation.video_project_id`, set by the session `POST` that starts one),
 * so every turn the agent takes carries its outline, the user's access tier
 * and what they are looking at. A save lands on the stage the moment it is
 * COMMITTED - the tools report it from inside their execution
 * (`tool_progress` / `presentation_saved` / `video_saved`, #3428) - so the
 * conversation and the preview stay one thing.
 *
 * What a studio is about comes from its `StudioSubject` (the host builds it:
 * `PresentationStudioRail`, `VideoStudioRail`): the binding, who can do the
 * job, what each message says about the view, the words, the suggestions.
 *
 * The agent picker offers only the agents that CARRY the subject's tool (and
 * the General Assistant when its tools do, `GET /web/chat/agents?studio=`).
 * The turn would add the tool to any agent (`ensure_studio_tools`), but an
 * agent whose persona was written for detection rules or STIX harvesting does
 * not become a deck editor by being handed the tool for one turn - it ignores
 * it, or declines the job - so the list is the agents built for it, and an
 * install with none says so instead of offering a conversation that cannot
 * edit.
 *
 * What is deliberately NOT here, compared with `/chat`: the conversation list
 * (the rail lists the subject's own conversations in its header), the session
 * tool picker (the agent's own configuration applies, plus the studio
 * guarantee), voice, `@mentions` of agents (a studio conversation has one
 * agent at a time, chosen up front, another taking it over in place) and
 * flows. "Open in chat" is one click away for anything else. Its `@` menu
 * offers conversations only.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowRightLeft,
  Bot,
  Check,
  ChevronsLeft,
  ChevronsRight,
  History,
  MessageSquareText,
  Plus,
  Sparkles,
} from "lucide-react";
import type { ChatPanelHandle } from "@filigran/chatbot";

import { LazyXtmChatPanel } from "@/components/chat-panel/LazyXtmChatPanel";
import { useCutTurnCards } from "@/components/chat-panel/cut-turn-cards";
import type { XtmChatPanelProps } from "@/components/chat-panel/XtmChatPanel";
import { GENERAL_ASSISTANT_ENTRY, studioPanelEndpoints, type StudioKind } from "@/components/chat-panel/endpoints";
import { useHostSender } from "@/components/chat-panel/host-send";
import { transcriptFooter } from "@/components/chat-panel/transcript-footer";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { ContinueWithAgentDialog } from "@/components/chat/ContinueWithAgentDialog";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useAwaitedWork,
  useConversation,
  useDismissInterruptedTurn,
  useStudioConversations,
  type StudioBindingField,
  type ToolProgressFrame,
} from "@/hooks/api/useChatApi";
import type { ToolAgents } from "@/hooks/useToolAgents";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { useResizableWidth } from "@/hooks/useResizableWidth";
import { useFormatter } from "@/i18n";
import { ICON_MAP } from "@/lib/constants";
import { conversationAgentId } from "@/lib/general-assistant-stand-in";
import { cn, timeAgo } from "@/lib/utils";
import { AwaitedWorkBanner } from "@/pages/chat/AwaitedWorkBanner";
import { useTurnFrames } from "@/pages/chat-panel/useTurnFrames";
import type { ChatMessage } from "@/types";
import { Spinner } from "@filigran/design-system";

/** What the studio's words say, in the reader's language (the host translates them). */
interface StudioLabels {
  /** The rail's accessible name ("Presentation Studio"). */
  rail: string;
  /** The history menu ("Conversations about this presentation"). */
  history: string;
  /** Who answers, when nobody can ("No agent with the deck tools"). */
  noAgentShort: string;
  /** The empty state's title when nobody can. */
  noAgentTitle: string;
  /** The empty state's text when nobody can (a new chat otherwise shows the panel's own empty state). */
  blockedHint: string;
  /** The toast's text when a `?c=` could not be used. */
  unavailableHint: string;
  /** The rail header's title; "Studio" when omitted (the fleet panel: "Modify with AI"). */
  title?: string;
  /** A line under the header naming what the rail changes (the fleet panel's binding line). */
  binding?: string;
  /** The composer's placeholder while it is ready; the panel's own when omitted. */
  placeholder?: string;
}

/** What every message tells the turn about the view. */
interface StudioSendContext {
  /** The 1-based slide / page on the stage. */
  studio_slide?: number;
  /** The scene under the playhead. */
  studio_scene?: string;
  /** The widget selected on the dashboard. */
  studio_widget?: string;
}

/** What a studio is about: the host's side of the rail. */
export interface StudioSubject {
  /** The studio, and the binding its conversations carry. */
  kind: StudioKind;
  field: StudioBindingField;
  id: string;
  title: string;
  /** Who can do the job (`useToolAgents` of the subject's tool). */
  toolAgents: ToolAgents;
  /** What every message tells the turn about the view (`studio_slide` / `studio_scene` / `studio_widget`). */
  sendContext: StudioSendContext;
  /** A tool's mid-execution report, for the host (the stage following the agent's pen). */
  onToolProgress?: (frame: ToolProgressFrame) => void;
  /** The deck on the stage, for the transcript's deck cards and slide links. */
  studioCards?: XtmChatPanelProps["studioCards"];
  /** Wraps the rail's panel (the video cards' context in the Video Studio). */
  wrapTranscript?: (transcript: ReactNode) => ReactNode;
  /**
   * A card under the transcript, read off the stored messages (the fleet
   * panel's confirm and refusal cards, #10162). `send` posts a message as the
   * user's, like the composer; `canSend` is false while a turn runs or the rail
   * cannot start one.
   */
  transcriptFooter?: (ctx: { messages: ChatMessage[]; send: (text: string) => void; canSend: boolean }) => ReactNode;
  /** The chip in the composer's toolbar: what the assistant is told the user is looking at. */
  focus: { label: string; tip: string };
  suggestions: string[];
  /**
   * The conversation that made the subject, offered while it has no studio
   * conversation yet: binding it brings the agent's memory of the work along.
   * `bind` rejects when the conversation is not the user's.
   */
  origin?: { conversationId: string | null; label: string; bind: (conversationId: string) => Promise<unknown>; pending: boolean };
  /** Extra attributes on the rail (the deck's keys stay off inside it). */
  railAttrs?: Record<string, string>;
  /** The `data-testid` prefix ("presentation.studio"). */
  testId: string;
  labels: StudioLabels;
}

/**
 * The rail's width is the reader's to set: a short exchange reads fine
 * in a narrow column, the outline of a whole deck or a slide-by-slide review
 * does not. The divider on the rail's right edge drags it out to half the
 * screen and back; the choice is remembered in the browser (it is a reading
 * preference, not part of the work or of the `?studio=1&c=` URL everyone
 * shares).
 */
/** Narrowest the rail stays usable at: header verbs, agent row, composer (px). */
const STUDIO_RAIL_MIN_WIDTH = 320;
/** Widest, as a share of the viewport: half the screen, the stage keeps the rest. */
const STUDIO_RAIL_MAX_FRACTION = 0.5;
/** `localStorage` key of the preferred width (px). */
const STUDIO_RAIL_WIDTH_KEY = "xtm-studio-rail-width";
/** Namespaces the panel's own browser storage (drafts) per studio. */
const STUDIO_STORAGE_PREFIX = "xtm-studio";
/** The width before anyone drags: the fixed widths the rail shipped with. */
const studioRailDefaultWidth = () =>
  typeof window !== "undefined" && window.matchMedia("(min-width: 1536px)").matches ? 440 : 380;
const studioRailMaxWidth = (viewport: number) => Math.floor(viewport * STUDIO_RAIL_MAX_FRACTION);

/**
 * The collapsed studio: a handle along the stage's left edge that brings the
 * rail out, so the chat is one click away without taking a third of the
 * screen from work the user only came to look at.
 */
export function StudioHandle({ onOpen, testId }: { onOpen: () => void; testId: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex h-full w-10 shrink-0 flex-col items-center border-r border-elevation-subtle-soft layer-1 bg-elevation-default py-2 print:hidden">
      <Tip content={t_i18n("Open the studio - edit with your assistant")} side="right">
        <Button
          variant="ghost"
          size="sm"
          onClick={onOpen}
          aria-label={t_i18n("Open the studio - edit with your assistant")}
          aria-expanded={false}
          className="h-8 w-8 px-0 text-accent hover:bg-accent/15"
          data-testid={`${testId}.handle`}
        >
          <Sparkles className="h-4 w-4" />
        </Button>
      </Tip>
      <span
        aria-hidden="true"
        className="mt-3 select-none font-heading content-caption uppercase tracking-[0.2em] text-default-secondary [writing-mode:vertical-rl]"
      >
        {t_i18n("Studio")}
      </span>
    </div>
  );
}

interface StudioRailProps {
  subject: StudioSubject;
  /** The conversation to open with (`?c=`), or `null` for the most recent
   *  studio conversation of this subject, or a fresh one when there is none. */
  initialConversationId: string | null;
  /** The rail's current conversation, for the URL. */
  onConversationChange: (id: string | null) => void;
  onCollapse: () => void;
  /**
   * Text to put in the composer, ready to send (the layout panel's `Fix with
   * my assistant`, #3410). Never sent on the user's behalf;
   * `onPrimedMessageApplied` is called once it is in the box so the host
   * clears it and a later remount does not paste it again.
   */
  primedMessage?: string | null;
  onPrimedMessageApplied?: () => void;
  /**
   * A message sent as the user's first one, as soon as the rail can start a
   * conversation: the request the "New video" dialog showed the user
   * before they created the video, so sending it is their act. Sent once;
   * `onAutoSendApplied` lets the host forget it.
   */
  autoSendMessage?: string | null;
  onAutoSendApplied?: () => void;
  /**
   * The agent a NEW conversation starts with, when the host already asked
   * (the "New video" dialog's "Made by"); `null` = the General Assistant.
   * Omitted: the rail's own default.
   */
  initialAgentId?: string | null;
  /**
   * Whether an agent turn is in flight in the rail's conversation.
   * The host forwards it to the presence heartbeat, which announces the flip
   * to collaborators without waiting for the next scheduled beat. Reports
   * `false` on unmount, so a collapsed rail never leaves a stale `true`.
   */
  onBusyChange?: (busy: boolean) => void;
  /**
   * Which side of the work the rail docks on. `"left"` (default): the
   * studios, the stage on the right. `"right"`: a page that keeps its own
   * layout on the left and docks the assistant beside it (the fleet page's
   * Modify with AI, #10162): the border, the resize divider and the collapse
   * chevron move to the rail's left edge.
   */
  side?: "left" | "right";
  /** The width before anyone drags (px, or read per render against the viewport); the studios' own default when omitted. */
  defaultWidth?: number | (() => number);
  className?: string;
}

export function StudioRail({
  subject,
  initialConversationId,
  onConversationChange,
  onCollapse,
  primedMessage = null,
  onPrimedMessageApplied,
  autoSendMessage = null,
  onAutoSendApplied,
  initialAgentId,
  onBusyChange,
  side = "left",
  defaultWidth,
  className,
}: StudioRailProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const chatSendEnabled = useChatSendEnabled();
  const aiOff = useAiActionDisabled();
  const chatOn = chatSendEnabled && !aiOff;
  const panelRef = useRef<ChatPanelHandle>(null);
  const hostSend = useHostSender(panelRef);

  // The rail's width: the reader's preference, clamped to half the screen.
  const docksRight = side === "right";
  const rail = useResizableWidth({
    defaultWidth: defaultWidth ?? studioRailDefaultWidth,
    minWidth: STUDIO_RAIL_MIN_WIDTH,
    maxWidth: studioRailMaxWidth,
    // A docked-right rail keeps its own width: the studios' preference is
    // read against a stage, this one against a page.
    storageKey: docksRight ? `${STUDIO_RAIL_WIDTH_KEY}-right` : STUDIO_RAIL_WIDTH_KEY,
    edge: docksRight ? "start" : "end",
  });

  const { data: agents } = useAgents();
  const { toolAgents, labels } = subject;
  const { data: history, isLoading: historyLoading } = useStudioConversations(subject.id, 50, subject.field);
  const endpoints = useMemo(() => studioPanelEndpoints(subject.kind, subject.id), [subject.kind, subject.id]);

  /* ---------- Which conversation ---------- */
  const [convId, setConvId] = useState<string | null>(initialConversationId);
  const selectConversation = useCallback(
    (id: string | null) => {
      setConvId(id);
      onConversationChange(id);
    },
    [onConversationChange],
  );
  // Landing without `?c=`: resume the subject's most recent studio conversation,
  // once — a later "New conversation" must not be undone by a refetch. The
  // recovery below re-arms it when the `?c=` it arrived with turns out to be
  // unusable, so the rail falls back on this subject's own history instead.
  const autoResumed = useRef(initialConversationId !== null);
  useEffect(() => {
    if (autoResumed.current || !history || convId) return;
    autoResumed.current = true;
    if (history.length > 0) selectConversation(history[0].id);
  }, [convId, history, selectConversation]);

  // A `?c=` is only a claim. One the caller cannot drive — a colleague's
  // conversation pasted along with the link (404 unless shared with them, and
  // then only readable), or one deleted since — the rail says so once and
  // starts over on this subject.
  const conversationQuery = useConversation(convId ?? "", { notFoundIsOrdinary: true });
  const { data: activeConv, isError: convMissing } = conversationQuery;
  const conversation = convId ? activeConv : undefined;
  const convUnusable = convMissing || activeConv?.access === "read";
  useEffect(() => {
    if (!convId || !convUnusable) return;
    toast({
      title: t_i18n("That conversation is not available here"),
      description: labels.unavailableHint,
      variant: "info",
    });
    autoResumed.current = false;
    selectConversation(null);
  }, [convId, convUnusable, labels.unavailableHint, selectConversation, t_i18n, toast]);

  const frames = useTurnFrames(convId, { onToolProgress: subject.onToolProgress });
  // Never left silent: the work the conversation waits on, and a last
  // turn that stopped before its answer.
  const { data: awaitedWork } = useAwaitedWork(convId, !convUnusable);
  const dismissInterrupted = useDismissInterruptedTurn(convId);

  // The turn in flight, for presence.
  const busy = frames.working;
  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  /* ---------- Which agent ---------- */
  // Once a conversation exists it has its agent, another of the studio's
  // taking it over in place ("Continue with another agent", #6734); the
  // panel's picker chooses for the NEXT one, among the agents the studio's
  // list offers. It starts on the host's pick, else the agent that already
  // worked on this subject here (while it still carries the tool), else the
  // shared default (`useToolAgents`).
  const { none: noStudioAgent } = toolAgents;
  const agentResolving = toolAgents.resolving || historyLoading;
  const [initialAgent, setInitialAgent] = useState<string | undefined>(undefined);
  useEffect(() => {
    if (initialAgent !== undefined || agentResolving) return;
    const recent = conversationAgentId(history?.find((c) => conversationAgentId(c)));
    const fallback =
      recent && toolAgents.agents.some((a) => a.id === recent) ? recent : toolAgents.defaultAgentId;
    const picked = initialAgentId === undefined ? fallback : initialAgentId;
    setInitialAgent(picked ?? GENERAL_ASSISTANT_ENTRY);
  }, [initialAgent, agentResolving, history, toolAgents.agents, toolAgents.defaultAgentId, initialAgentId]);

  // No conversation yet and nothing to start one with: the rail says so
  // rather than offering a General Assistant that cannot do the job.
  const startBlocked = !convId && noStudioAgent;
  const readOnly = !chatOn || startBlocked || convUnusable;

  /* ---------- Launchers: a primed message, the "New video" brief ---------- */
  const [panelReady, setPanelReady] = useState(false);
  const onReady = useCallback(() => setPanelReady(true), []);
  // A message primed by the host lands in the composer, ready for the user to
  // read and send. Each prime lands ONCE: the host clears it on
  // `onPrimedMessageApplied`, but only on its next render.
  const appliedPrime = useRef<string | null>(null);
  useEffect(() => {
    if (!primedMessage) {
      appliedPrime.current = null;
      return;
    }
    if (!panelReady || appliedPrime.current === primedMessage) return;
    appliedPrime.current = primedMessage;
    panelRef.current?.setDraft(primedMessage);
    panelRef.current?.focusComposer();
    onPrimedMessageApplied?.();
  }, [primedMessage, panelReady, onPrimedMessageApplied]);
  // The brief goes out once the panel can start the conversation, as a typed
  // message would; one that could not be sent is left in the composer.
  const autoSent = useRef<string | null>(null);
  useEffect(() => {
    if (!autoSendMessage || autoSent.current === autoSendMessage || !panelReady || readOnly) return;
    autoSent.current = autoSendMessage;
    onAutoSendApplied?.();
    void hostSend.send(autoSendMessage);
  }, [autoSendMessage, panelReady, readOnly, onAutoSendApplied, hostSend]);

  /* ---------- What the panel carries for the studio ---------- */
  const { sendContext } = subject;
  const getRequestExtras = useCallback(() => ({ ...sendContext }), [sendContext]);

  const [historyOpen, setHistoryOpen] = useState(false);
  const historyRef = useRef<HTMLDivElement>(null);
  useClickOutside(historyRef, () => setHistoryOpen(false), historyOpen);
  const startNewConversation = useCallback(() => {
    selectConversation(null);
    setHistoryOpen(false);
    panelRef.current?.focusComposer();
  }, [selectConversation]);

  const origin = subject.origin;
  const originConvId = origin?.conversationId ?? null;
  const [originUnavailable, setOriginUnavailable] = useState(false);
  const offerOrigin = !!originConvId && !originUnavailable && (history?.length ?? 0) === 0 && !convId;
  const continueOrigin = useCallback(async () => {
    if (!originConvId || !origin) return;
    try {
      await origin.bind(originConvId);
      selectConversation(originConvId);
    } catch {
      setOriginUnavailable(true);
    }
  }, [origin, originConvId, selectConversation]);

  // Another of the studio's agents takes the conversation over in place, its
  // history kept: a web conversation, between two turns.
  const [continueWithAgentOpen, setContinueWithAgentOpen] = useState(false);
  const canContinueWithAgent = !!conversation && !convUnusable && conversation.channel === "web";

  const headerActions = (
    <div className="flex items-center gap-0.5">
      <div ref={historyRef} className="relative">
        <Tip content={labels.history}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setHistoryOpen((o) => !o)}
            aria-label={labels.history}
            data-testid={`${subject.testId}.history`}
            aria-haspopup="menu"
            aria-expanded={historyOpen}
            className={cn("h-8 w-8 px-0", historyOpen && "bg-accent/15 text-accent")}
          >
            <History className="h-4 w-4" />
          </Button>
        </Tip>
        {historyOpen && (
          <div
            role="menu"
            className="absolute right-0 top-[calc(100%+4px)] z-50 w-72 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default p-1 text-default-primary shadow-overlay animate-in fade-in-0 zoom-in-95"
          >
            <button
              type="button"
              role="menuitem"
              onClick={startNewConversation}
              disabled={!chatOn || noStudioAgent}
              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left content-compact text-accent transition-colors hover:bg-elevation-hover disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5 shrink-0" />
              {t_i18n("New conversation")}
            </button>
            {(history?.length ?? 0) > 0 && <div className="my-1 border-t border-elevation-subtle-soft" />}
            <div className="max-h-[280px] overflow-y-auto">
              {(history ?? []).map((c) => {
                const answeringId = conversationAgentId(c);
                const Icon = (c.agent_icon && ICON_MAP[c.agent_icon]) || (answeringId ? Bot : Sparkles);
                return (
                  <button
                    type="button"
                    role="menuitem"
                    key={c.id}
                    onClick={() => {
                      selectConversation(c.id);
                      setHistoryOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left content-compact transition-colors hover:bg-elevation-hover",
                      c.id === convId && "bg-elevation-hover",
                    )}
                  >
                    <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{c.title}</span>
                      <span className="block truncate content-caption text-default-secondary">
                        {c.agent_name ?? agents?.find((a) => a.id === answeringId)?.name ?? t_i18n("General Assistant")}{" "}
                        · {timeAgo(c.updated_at)}
                      </span>
                    </span>
                    {c.id === convId && <Check className="mt-0.5 h-3 w-3 shrink-0 text-accent" />}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
      {canContinueWithAgent && (
        <Tip content={t_i18n("Continue with another agent")}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setContinueWithAgentOpen(true)}
            disabled={!chatOn || frames.working}
            aria-label={t_i18n("Continue with another agent")}
            data-testid={`${subject.testId}.continue-with-agent`}
            className="h-8 w-8 px-0"
          >
            <ArrowRightLeft className="h-4 w-4" />
          </Button>
        </Tip>
      )}
      {convId && (
        <Tip content={t_i18n("Open in chat")}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate(`/chat?c=${convId}`)}
            aria-label={t_i18n("Open in chat")}
            className="h-8 w-8 px-0"
          >
            <MessageSquareText className="h-4 w-4" />
          </Button>
        </Tip>
      )}
    </div>
  );

  // The empty state is the panel's own on every surface (/chat, the rails, an
  // agent's conversation): the agent's mark, one greeting line naming it, its
  // suggestions as design-system chips (UX retest 09a3805e, F1). The rail only
  // draws its own screen when no conversation can start at all.
  const welcome = startBlocked || aiOff ? (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 px-5 py-6 text-center">
      <div className="space-y-1">
        <p className="font-heading content-base font-medium">{labels.noAgentTitle}</p>
        {!aiOff && <p className="content-compact leading-relaxed text-default-secondary">{labels.blockedHint}</p>}
      </div>
      {aiOff ? (
        <AiDisabledNotice />
      ) : (
        <Button variant="outline" size="sm" onClick={() => navigate("/agents")} className="gap-1.5">
          <Bot className="h-3.5 w-3.5" />
          {t_i18n("Open agents")}
        </Button>
      )}
    </div>
  ) : undefined;

  // The conversation that made the subject, offered above a new chat while the
  // subject has no studio conversation yet.
  const originOffer =
    offerOrigin && origin ? (
      <div className="flex justify-center px-3 py-2">
        <button
          type="button"
          onClick={() => void continueOrigin()}
          disabled={origin.pending || !chatOn}
          className="inline-flex items-center gap-1.5 content-compact text-accent underline-offset-2 hover:underline disabled:opacity-50"
        >
          {origin.pending && <Spinner size="sm" tone="inherit" />}
          {!origin.pending && <MessageSquareText className="h-3 w-3" />}
          {origin.label}
        </button>
      </div>
    ) : null;

  // A turn the platform cut, and the replies that resumed it, read as ONE card
  // in the rail as on /chat (`useCutTurnCards`): in place of the cut reply,
  // and under the transcript while the platform resumes it (#10910).
  const storedMessages = conversation?.messages;
  const resumeHere = convUnusable ? undefined : () => void hostSend.send(t_i18n("Continue where you left off."));
  const dismissHere = convUnusable ? undefined : () => dismissInterrupted.mutate();
  const reloadPanelTranscript = useCallback(() => panelRef.current?.reloadConversation(), []);
  const cutTurns = useCutTurnCards(storedMessages, {
    resumingTurn: frames.resumingTurn,
    interruptedTurn: conversation?.interrupted_turn,
    working: frames.working,
    reloadTranscript: reloadPanelTranscript,
    onResume: resumeHere,
    onDismiss: dismissHere,
    resumeDisabled: readOnly,
  });
  const footer = transcriptFooter({
    blocks: [cutTurns.liveCard],
    working: frames.working,
    // A transcript ending with a cut says it on the cut's card instead.
    resumingTurn: cutTurns.endsWithCut ? null : frames.resumingTurn,
    wakingTurn: frames.wakingTurn,
    waitItems: frames.waitItems,
    interruptedTurn: cutTurns.endsWithCut ? null : conversation?.interrupted_turn,
    onResume: resumeHere,
    onDismiss: dismissHere,
    resumeDisabled: readOnly,
  });
  // A host card's answer is an ordinary message of the user, sent with the view's context.
  const hostCard =
    storedMessages && storedMessages.length > 0
      ? subject.transcriptFooter?.({
          messages: storedMessages,
          send: (text) => void hostSend.send(text),
          canSend: !readOnly && !frames.working,
        })
      : null;

  // The chip takes the room the toolbar leaves and reads at the 12 px floor; a
  // name still cut is said whole in its tooltip, above what the assistant is told.
  // It is a tab stop so the keyboard reaches that tooltip, and it draws the
  // design-system focus ring (2 px ring-focus, 2 px offset on the surface).
  const focusChip = (
    <Tip
      content={
        <>
          <span className="block font-medium">{subject.focus.label}</span>
          {subject.focus.tip}
        </>
      }
    >
      <span
        tabIndex={0}
        data-testid="studio-rail.focus-chip"
        className="inline-flex h-6 min-w-0 max-w-[220px] items-center rounded-sm border border-elevation-subtle-soft px-1.5 content-compact text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-focus"
      >
        <span className="truncate">{subject.focus.label}</span>
      </span>
    </Tip>
  );

  const panelWrapper = subject.wrapTranscript ?? ((node: ReactNode) => node);
  const railLoading = <RailLoading />;

  return (
    <aside
      {...subject.railAttrs}
      className={cn(
        "relative flex h-full shrink-0 flex-col border-elevation-subtle-soft bg-elevation-default print:hidden",
        docksRight ? "border-l" : "border-r",
        rail.isResizing && "select-none",
        className,
      )}
      style={{ width: rail.width }}
      aria-label={labels.rail}
      data-testid={`${subject.testId}.rail`}
    >
      {/* The divider: drag it to widen the rail (to half the screen), arrows
          nudge it, double-click resets. Left out when the viewport leaves no
          room between the bounds (`canResize` false). */}
      {rail.canResize && (
        <Tip content={t_i18n("Drag to resize the studio - double-click to reset")} side={docksRight ? "left" : "right"}>
          <div
            {...rail.handleProps}
            aria-label={t_i18n("Resize the studio")}
            data-testid={`${subject.testId}.resize`}
            data-side={side}
            className={cn(
              "group absolute inset-y-0 z-20 w-2 cursor-col-resize touch-none outline-none",
              docksRight ? "-left-1" : "-right-1",
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                "absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-[width,background-color,box-shadow] duration-150",
                rail.isResizing
                  ? "w-0.5 bg-accent shadow-[0_0_12px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]"
                  : "bg-transparent group-hover:w-0.5 group-hover:bg-accent/70 group-focus-visible:w-0.5 group-focus-visible:bg-accent group-focus-visible:shadow-[0_0_8px_color-mix(in_srgb,_var(--color-accent)_calc(0.6_*_100%),_transparent)]",
              )}
            />
          </div>
        </Tip>
      )}
      <header className="flex h-12 shrink-0 items-center gap-1 border-b border-elevation-subtle-soft px-2">
        <Tip content={t_i18n("Collapse the studio")}>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCollapse}
            aria-label={t_i18n("Collapse the studio")}
            className="h-8 w-8 px-0"
          >
            {docksRight ? <ChevronsRight className="h-4 w-4" /> : <ChevronsLeft className="h-4 w-4" />}
          </Button>
        </Tip>
        <Sparkles className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <span className="truncate font-heading content-base font-medium">{labels.title ?? t_i18n("Studio")}</span>
      </header>
      {labels.binding && (
        <p
          className="shrink-0 truncate border-b border-elevation-subtle-soft px-3 py-1.5 content-compact text-default-secondary"
          title={labels.binding}
          data-testid={`${subject.testId}.binding`}
        >
          {labels.binding}
        </p>
      )}

      {initialAgent === undefined ? (
        railLoading
      ) : (
        <div className="flex min-h-0 flex-1" data-testid={`${subject.testId}.panel`}>
          {panelWrapper(
            <LazyXtmChatPanel
              fallback={railLoading}
              ref={panelRef}
              apiEndpoints={endpoints}
              storedMessages={conversation?.messages}
              replaceMessage={cutTurns.replaceMessage}
              renderAboveMessage={cutTurns.renderAboveMessage}
              resumedShownAbove={cutTurns.resumedIds}
              working={frames.working}
              storageKeyPrefix={`${STUDIO_STORAGE_PREFIX}-${subject.kind}`}
              conversationId={convId}
              onConversationChange={selectConversation}
              initialAgent={initialAgent}
              readOnly={readOnly}
              readOnlyNotice={aiOff ? <AiDisabledNotice /> : chatOn ? labels.noAgentShort : t_i18n("Chat is unavailable")}
              getRequestExtras={getRequestExtras}
              onStreamEvent={frames.onStreamEvent}
              onTurnRunningChange={frames.onTurnRunningChange}
              onReady={onReady}
              onHeldSendSettled={hostSend.onHeldSendSettled}
              studioCards={subject.studioCards}
              headerActions={headerActions}
              banner={
                frames.working ? null : (
                  <>
                    <AwaitedWorkBanner items={awaitedWork ?? []} className="px-3" />
                    {originOffer}
                  </>
                )
              }
              composerToolbar={focusChip}
              transcriptFooter={
                hostCard ? (
                  <>
                    {footer}
                    {hostCard}
                  </>
                ) : (
                  footer
                )
              }
              placeholder={labels.placeholder}
              welcome={welcome}
              // The studio's own prompts, shown as the panel's chips when the
              // picked agent serves none of its own.
              promptSuggestions={subject.suggestions}
              hideConversationList
              steerAttachments
              holdSendsDuringTurn
            />,
          )}
        </div>
      )}
      <ContinueWithAgentDialog
        conversationId={convId}
        currentAgentId={conversation?.agent_id ?? null}
        deletedAgentName={conversation?.agent_id ? null : (conversation?.deleted_agent_name ?? null)}
        candidates={toolAgents.agents}
        offerGeneralAssistant={toolAgents.generalAssistantOffered}
        open={continueWithAgentOpen}
        onClose={() => setContinueWithAgentOpen(false)}
        panel={panelRef}
      />
    </aside>
  );
}

/** While who answers, or the panel itself, is still loading. */
function RailLoading() {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 p-3" aria-busy>
      <Skeleton className="h-7 w-40" />
      <div className="flex flex-1 items-center justify-center">
        <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    </div>
  );
}
