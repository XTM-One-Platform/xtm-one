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
 * The task pane's chat: the shared `@filigran/chatbot` panel over the web chat's routes,
 * with what is Office's own: the document's context on every question, quick actions,
 * and Replace / Insert / Comment under an answer, never applied without the user's click.
 */
import "@filigran/chatbot/styles.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChatPanel,
  type ChatMessage as PanelMessage,
  type ChatMessageSlotContext,
  type ChatPanelHandle,
  type ChatSendDraft,
  type ChatSendOutcome,
  type ChatTurnEnd,
} from "@filigran/chatbot";
import {
  Calculator,
  Check,
  Code,
  FileText,
  Languages,
  Lightbulb,
  LogOut,
  MessageSquare,
  Moon,
  Pencil,
  Plus,
  RotateCcw,
  Sparkles,
  Sun,
  Table,
  X,
} from "lucide-react";
import { getBaseUrl, getToken } from "@/lib/api";
import { extractAction, stripActionBlock } from "@/lib/actions";
import { renderAgentIcon } from "@/lib/agentIcons";
import { agentPageUrl, openAgentPage } from "@/lib/agentPage";
import { applyAgentAction, getDocumentContext, wordGetDocumentChunks, wordGetFullText } from "@/lib/office";
import {
  OFFICE_PANEL_ENDPOINTS,
  OFFICE_PANEL_FILE_LIMITS,
  OFFICE_PANEL_STORAGE_PREFIX,
  OFFICE_PANEL_THEME,
  officePanelTranslate,
  panelApiBaseUrl,
} from "@/lib/panelConfig";
import {
  FULL_DOC_ACTIONS,
  buildChunkCommentPrompt,
  buildDocumentPrompt,
  buildFullDocumentPrompt,
  chunkReviewShown,
  getAvailableActions,
  isFullDocRequest,
  quickActionPrompt,
  shownRequestOf,
} from "@/lib/prompts";
import { createTypedSender } from "@/lib/sends";
import { createTurnWatch, runOnePerTurn, type TurnWatch } from "@/lib/turns";
import type { ActionPayload, DocumentChunk, DocumentContext, OfficeHostApp, Theme, User } from "@/lib/types";
import { ApplyPreview } from "./ApplyPreview";

/**
 * The pane's accent, its stylesheet's `--cyan` for the theme on screen: the
 * panel takes a colour, not a token. `useTheme` sets the theme's attribute
 * after this renders, so the value is read again when it changes.
 */
function usePaneAccent(): string | undefined {
  const read = () => getComputedStyle(document.documentElement).getPropertyValue("--cyan").trim() || undefined;
  const [accent, setAccent] = useState(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setAccent(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return accent;
}

/**
 * The first name the greeting addresses, as the web chat reads it: the
 * account's `full_name`, else its `name`, else the part of the email before
 * the `@`, so the line never ends on "help you, ?".
 */
function greetingName(user: Pick<User, "email" | "full_name" | "name">): string {
  const display = (user.full_name || user.name || "").trim();
  if (display) return display.split(/\s+/)[0];
  return (user.email || "").split("@")[0].trim();
}

/**
 * The pane's body font (IBM Plex Sans from its stylesheet), passed to the panel
 * as the web host does, so the chips, header and composer do not fall back to
 * the package's system font.
 */
function usePaneFont(): string | undefined {
  return useMemo(() => getComputedStyle(document.body).fontFamily || undefined, []);
}

type ApplyStatus = { state: "applied" } | { state: "discarded" } | { state: "failed"; reason: string };

/** The button an answer's `action` block puts under it. */
const BLOCK_ACTION_LABELS: Record<ActionPayload["action"], string> = {
  replace_selection: "Replace",
  insert_at_end: "Insert below",
  insert_paragraph: "Insert paragraph",
  insert_table: "Insert table",
  insert_html: "Insert",
  add_comment: "Comment",
};

/** What a quick action read of the document before it sent anything. */
interface QuickActionRead {
  ctx: DocumentContext;
  /** The whole document, for an action on it with nothing selected. */
  fullText?: string;
  /** The document's sections, for a review commented one turn per section. */
  chunks?: DocumentChunk[];
}

async function readForQuickAction(hostApp: OfficeHostApp, actionId: string): Promise<QuickActionRead> {
  const ctx = await getDocumentContext(hostApp);
  if (hostApp === "Word" && FULL_DOC_ACTIONS.has(actionId) && !ctx.selection.text) {
    return { ctx, fullText: await wordGetFullText() };
  }
  if (hostApp === "Word" && actionId === "comment_full") return { ctx, chunks: await wordGetDocumentChunks() };
  return { ctx };
}

/** What the sign-in screen says when the server ended the session. */
function sessionEndedMessage(reason: string | undefined): string {
  return reason === "idle"
    ? "You were signed out after a period of inactivity. Sign in again to continue."
    : "Your session has expired. Sign in again to continue.";
}

interface OfficeChatPanelProps {
  user: User;
  hostApp: OfficeHostApp;
  /** The document's context as of the last selection change (`useOffice`). */
  context: DocumentContext | null;
  /** Read the document's context again. */
  onRefreshContext: () => unknown;
  theme: Theme;
  onToggleTheme: () => void;
  /** Sign out; `reason` is what the sign-in screen says. */
  onSignOut: (reason?: string) => void;
}

export function OfficeChatPanel({
  user,
  hostApp,
  context,
  onRefreshContext,
  theme,
  onToggleTheme,
  onSignOut,
}: OfficeChatPanelProps) {
  const panelRef = useRef<ChatPanelHandle>(null);
  const token = getToken();
  const requestHeaders = useMemo(() => (token ? { Authorization: `Bearer ${token}` } : undefined), [token]);
  const apiBaseUrl = useMemo(() => panelApiBaseUrl(getBaseUrl()), []);
  // The switcher's "Open agent page": the platform's page of the agent, in a
  // new window (the pane has no agent route), as the web chat's agentPageHref.
  const agentHref = useCallback((agent: { id: string }) => agentPageUrl(getBaseUrl(), agent.id), []);
  const onAgentNameClick = useCallback((agent: { id: string }) => {
    const url = agentPageUrl(getBaseUrl(), agent.id);
    if (url) openAgentPage(url);
  }, []);
  const firstName = greetingName(user);
  const look = usePaneFont();
  const panelUser = useMemo(() => ({ firstName }), [firstName]);
  const actions = useMemo(() => getAvailableActions(hostApp), [hostApp]);
  const accent = usePaneAccent();

  /* ---------- The turn on screen ---------- */
  // Created by the effect, so a remount (StrictMode) gets a watch of its own.
  const turns = useRef<TurnWatch | null>(null);
  useEffect(() => {
    const watch = createTurnWatch();
    turns.current = watch;
    return () => {
      watch.close();
      if (turns.current === watch) turns.current = null;
    };
  }, []);
  const onTurnRunningChange = useCallback(
    (now: boolean, info?: ChatTurnEnd) => {
      turns.current?.setRunning(now, info?.ended);
      // The answer may have changed what is selected or what surrounds it.
      if (!now) onRefreshContext();
    },
    [onRefreshContext],
  );
  const onSessionEnded = useCallback((reason: string | undefined) => onSignOut(sessionEndedMessage(reason)), [onSignOut]);

  /* ---------- What a send carries, and what the user is shown ---------- */
  const contextRef = useRef(context);
  contextRef.current = context;

  /** A question the pane built, under the words the user's bubble shows. */
  const sendShown = useCallback(
    async (text: string, shown: string): Promise<ChatSendOutcome | undefined> =>
      panelRef.current?.send(text, { displayText: shown }),
    [],
  );

  // The composer keeps the text a taken-over send stands for until the send
  // lands (`lib/sends.ts`), a message held while a turn ran included.
  const [typedSender] = useState(() => createTypedSender(() => panelRef.current));

  const onBeforeSend = useCallback(
    (draft: ChatSendDraft) => {
      // A steer, a spoken phrase or the pane's own send goes as said.
      if (draft.source !== "composer" || draft.turnRunning) return;
      const typed = draft.text;
      if (hostApp === "Word" && isFullDocRequest(typed)) {
        // The whole document is read first: the send is taken over and goes
        // through the handle once it is in.
        void typedSender.send(typed, async () => {
          const ctx = contextRef.current ?? (await getDocumentContext(hostApp));
          const fullText = await wordGetFullText().catch(() => "");
          return sendShown(buildFullDocumentPrompt(hostApp, fullText, ctx.documentMeta, typed), typed);
        });
        return false;
      }
      const ctx = contextRef.current;
      return ctx ? { text: buildDocumentPrompt(ctx, typed), displayText: typed } : undefined;
    },
    [hostApp, sendShown, typedSender],
  );

  // The review under way, if any: a new one replaces it (the watch closing
  // with the pane ends it too).
  const review = useRef<{ cancelled: boolean } | null>(null);

  // Why the last quick action sent nothing: Office could not read the document.
  const [readFailure, setReadFailure] = useState<string | null>(null);

  const runQuickAction = useCallback(
    async (actionId: string) => {
      setActionsOpen(false);
      setReadFailure(null);
      const { prompt, label } = quickActionPrompt(actionId);
      // The document is read before anything is sent: a read Office refuses
      // (protected view, a co-authoring lock, a closed document) sends nothing
      // and says so above the composer.
      let read: QuickActionRead;
      try {
        read = await readForQuickAction(hostApp, actionId);
      } catch (err) {
        console.error("Quick action could not read the document:", err);
        setReadFailure(err instanceof Error ? err.message : String(err));
        return;
      }
      const { ctx, fullText, chunks } = read;
      if (fullText !== undefined) {
        await sendShown(buildFullDocumentPrompt(hostApp, fullText, ctx.documentMeta, prompt, actionId), label);
      } else if (chunks) {
        // One turn per section (runOnePerTurn).
        const watch = turns.current;
        if (!watch) return;
        if (review.current) review.current.cancelled = true;
        const current = { cancelled: false };
        review.current = current;
        const steps = chunks.map((chunk, i) => ({
          text: buildChunkCommentPrompt(hostApp, chunk, chunks.length, prompt),
          displayText: chunkReviewShown(i, chunks.length),
        }));
        await runOnePerTurn(steps, (step) => sendShown(step.text, step.displayText), watch, () => current.cancelled);
        if (review.current === current) review.current = null;
      } else {
        await sendShown(buildDocumentPrompt(ctx, prompt, actionId), label);
      }
    },
    [hostApp, sendShown],
  );

  /* ---------- Putting an answer into the document ---------- */
  const [applyStatus, setApplyStatus] = useState<Record<string, ApplyStatus>>({});
  const [preview, setPreview] = useState<{ messageId: string; original: string; proposed: string } | null>(null);
  const apply = useCallback(
    async (messageId: string, action: string, content: string) => {
      try {
        await applyAgentAction(hostApp, action, content);
        setApplyStatus((s) => ({ ...s, [messageId]: { state: "applied" } }));
        onRefreshContext();
      } catch (err) {
        console.error("Apply failed:", err);
        const reason = err instanceof Error ? err.message : String(err);
        setApplyStatus((s) => ({ ...s, [messageId]: { state: "failed", reason } }));
      }
    },
    [hostApp, onRefreshContext],
  );

  const renderMessageExtras = useCallback(
    (message: PanelMessage, { streaming }: ChatMessageSlotContext) => {
      if (message.role !== "assistant") return null;
      // A reply still being written, or a note written in place of an agent
      // that did not answer: not document content.
      if (streaming) return null;
      if (message.mentionFailed) return null;
      const payload = extractAction(message.content);
      const content = payload?.content || stripActionBlock(message.content);
      if (!content) return null;
      const status = applyStatus[message.id]?.state;
      if (status === "applied") {
        return (
          <div className="inline-feedback inline-feedback-success">
            <Check size={12} />
            Applied to document
          </div>
        );
      }
      if (status === "discarded") return <div className="inline-feedback inline-feedback-muted">Dismissed</div>;
      const failed = applyStatus[message.id];
      const failure =
        failed?.state === "failed" ? (
          <div className="inline-feedback inline-feedback-muted">Could not apply: {failed.reason}</div>
        ) : null;
      const discard = (
        <button
          className="inline-action-btn inline-action-muted"
          onClick={() => setApplyStatus((s) => ({ ...s, [message.id]: { state: "discarded" } }))}
          aria-label="Discard"
        >
          <X size={11} />
        </button>
      );
      if (payload) {
        // The agent chose what to do with its content: that is what applies.
        const { action } = payload;
        const label = BLOCK_ACTION_LABELS[action] ?? action.replace(/_/g, " ");
        return (
          <>
            {failure}
            <div className="inline-actions">
              <button
                className="inline-action-btn inline-action-primary"
                onClick={() =>
                  action === "replace_selection"
                    ? setPreview({ messageId: message.id, original: contextRef.current?.selection.text ?? "", proposed: content })
                    : void apply(message.id, action, content)
                }
              >
                {action === "replace_selection" ? <Pencil size={11} /> : action === "add_comment" ? <MessageSquare size={11} /> : <Plus size={11} />}
                {label}
              </button>
              {discard}
            </div>
          </>
        );
      }
      return (
        <>
          {failure}
          <div className="inline-actions">
            <button
              className="inline-action-btn inline-action-primary"
              onClick={() =>
                setPreview({ messageId: message.id, original: contextRef.current?.selection.text ?? "", proposed: content })
              }
            >
              <Pencil size={11} />
              Replace
            </button>
            {hostApp !== "Excel" && (
              <button className="inline-action-btn" onClick={() => void apply(message.id, "insert_at_end", content)}>
                <Plus size={11} />
                Insert below
              </button>
            )}
            {(hostApp === "Word" || hostApp === "Excel") && (
              <button className="inline-action-btn" onClick={() => void apply(message.id, "add_comment", content)}>
                <MessageSquare size={11} />
                Comment
              </button>
            )}
            {discard}
          </div>
        </>
      );
    },
    [applyStatus, hostApp, apply],
  );

  // The action block is the pane's to apply, never text to read. A question
  // read back from the conversation is the prompt the pane built (the words
  // its bubble showed are not stored): it shows what the user asked again.
  const transformMessageText = useCallback(
    (text: string, message: PanelMessage) =>
      message.role === "assistant" ? stripActionBlock(text) : message.role === "user" ? shownRequestOf(text) : text,
    [],
  );

  /* ---------- The pane's own pieces in the panel ---------- */
  const [actionsOpen, setActionsOpen] = useState(false);
  const selection = context?.selection;
  const selectionPill = selection?.text ? (
    <div className="selection-pill">
      <FileText size={11} />
      <span className="selection-pill-text">
        {selection.text.slice(0, 60)}
        {selection.text.length > 60 && "..."}
      </span>
      <span className="selection-pill-loc">{selection.location}</span>
    </div>
  ) : null;
  const readFailureNotice = readFailure ? (
    <div className="pane-notice" role="alert">
      <span className="pane-notice-text">Could not read the document: {readFailure}</span>
      <button className="btn-icon" onClick={() => setReadFailure(null)} aria-label="Dismiss">
        <X size={12} />
      </button>
    </div>
  ) : null;
  const composerHeader =
    readFailureNotice || selectionPill ? (
      <>
        {readFailureNotice}
        {selectionPill}
      </>
    ) : null;

  const composerToolbar = (
    <div style={{ position: "relative" }}>
      {actionsOpen && (
        <div className="action-dropdown">
          {actions.map((a) => (
            <button key={a.id} className="action-dropdown-item" onClick={() => void runQuickAction(a.id)}>
              <ActionIcon name={a.icon} size={14} />
              <div>
                <div style={{ fontWeight: 500 }}>{a.label}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{a.description}</div>
              </div>
            </button>
          ))}
        </div>
      )}
      <button
        className="btn-icon compose-action-toggle"
        onClick={() => setActionsOpen((open) => !open)}
        aria-label="Quick actions"
        aria-expanded={actionsOpen}
      >
        {actionsOpen ? <X size={16} /> : <Sparkles size={16} style={{ color: "var(--cyan)" }} />}
      </button>
    </div>
  );

  // The new conversation is the panel's own empty state, as on the web chat:
  // the agent's mark, one line naming the agent and the user, and the
  // suggestions as design-system chips. The host's list is the pane's first
  // quick actions, in the words a user would type (a click puts it in the
  // composer, and the send carries the document as any typed question); an
  // agent that serves suggestions of its own shows those instead.
  const promptSuggestions = useMemo(
    () => actions.slice(0, 4).map((a) => quickActionPrompt(a.id).label),
    [actions],
  );

  const headerActions = (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <span className="badge badge-cyan">{hostApp}</span>
      <button className="btn-icon" onClick={onToggleTheme} aria-label="Switch theme">
        {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
      </button>
      <button className="btn-icon" onClick={() => onSignOut()} aria-label="Sign out">
        <LogOut size={14} />
      </button>
    </div>
  );

  return (
    <div className={theme === "dark" ? "dark" : undefined} style={{ display: "flex", flex: 1, minHeight: 0 }}>
      <ChatPanel
        ref={panelRef}
        mode="inline"
        backendType="rest"
        apiBaseUrl={apiBaseUrl}
        apiEndpoints={OFFICE_PANEL_ENDPOINTS}
        requestHeaders={requestHeaders}
        user={panelUser}
        accentColor={accent}
        fontFamily={look}
        theme={OFFICE_PANEL_THEME}
        t={officePanelTranslate}
        storageKeyPrefix={OFFICE_PANEL_STORAGE_PREFIX}
        {...OFFICE_PANEL_FILE_LIMITS}
        hideConversationList
        onBeforeSend={onBeforeSend}
        onHeldSendSettled={typedSender.onHeldSendSettled}
        onSessionEnded={onSessionEnded}
        onTurnRunningChange={onTurnRunningChange}
        transformMessageText={transformMessageText}
        renderMessageExtras={renderMessageExtras}
        composerHeader={composerHeader}
        composerToolbar={composerToolbar}
        promptSuggestions={promptSuggestions}
        renderAgentIcon={renderAgentIcon}
        agentHref={agentHref}
        onAgentNameClick={onAgentNameClick}
        headerActions={headerActions}
        steerAttachments
        holdSendsDuringTurn
      />
      {preview && (
        <ApplyPreview
          original={preview.original}
          proposed={preview.proposed}
          onCancel={() => setPreview(null)}
          onApply={() => {
            const { messageId, proposed } = preview;
            setPreview(null);
            void apply(messageId, "replace_selection", proposed);
          }}
        />
      )}
    </div>
  );
}

function ActionIcon({ name, size }: { name: string; size: number }) {
  const props = { size };
  switch (name) {
    case "FileText":
      return <FileText {...props} />;
    case "Lightbulb":
      return <Lightbulb {...props} />;
    case "Languages":
      return <Languages {...props} />;
    case "Calculator":
      return <Calculator {...props} />;
    case "Pencil":
      return <Pencil {...props} />;
    case "MessageSquare":
      return <MessageSquare {...props} />;
    case "Table":
      return <Table {...props} />;
    case "Code":
      return <Code {...props} />;
    case "RotateCcw":
      return <RotateCcw {...props} />;
    default:
      return <Sparkles {...props} />;
  }
}
