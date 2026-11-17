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
 * The routes the `@filigran/chatbot` panel talks to on XTM One, and the agent
 * picker's entries that are not agents. One module for every surface that
 * shows a conversation (/chat, the studio rails, an agent's conversation).
 */
import type { ApiEndpoints } from "@filigran/chatbot";
import { API_BASE } from "@/lib/api";
import { stripBasePath } from "@/lib/base-path";

/** The native chat's file route, relative to `API_BASE`: a file downloads at `{route}/{id}/download`. */
const CHAT_FILES_ROUTE = "/chat/files";

/**
 * The web chat's routes (`/api/v1/web`, HTTP + SSE, `chat_web_stream.py`),
 * relative to `API_BASE`. Files download and read aloud through the native
 * chat routes, which take the same session.
 */
export const CHAT_PANEL_ENDPOINTS: ApiEndpoints = {
  agents: "/web/chat/agents",
  messages: "/web/chat/messages",
  steer: "/web/chat/messages/steer",
  approve: "/web/chat/messages/approve",
  cancel: "/web/chat/messages/cancel",
  skipWait: "/web/chat/messages/skip-wait",
  pendingApprovals: "/web/chat/conversations",
  events: "/web/chat/conversations",
  sessions: "/web/chat/sessions",
  history: "/web/chat/sessions",
  messagesPage: "/web/chat/sessions",
  clearAll: "/web/chat/sessions",
  upload: "/web/chat/upload",
  prompts: "/web/chat/prompts",
  suggestions: "/web/chat/suggestions",
  workspaces: "/web/chat/workspaces",
  conversationReferences: "/web/chat/conversation-references",
  artifacts: "/web/chat/artifacts",
  download: CHAT_FILES_ROUTE,
  tts: "/chat/tts",
  // The agentic quota of the composer, the panel's own indicator: today by
  // default, a click opens today, this month and this year (`periods`).
  quota: "/web/chat/quota",
};

/** A studio rail's subject: the deck, the video, the dashboard or the fleet its conversations are bound to. */
export type StudioKind = "presentation" | "video" | "dashboard" | "fleet";
const STUDIO_BINDING: Record<StudioKind, string> = {
  presentation: "presentation_id",
  video: "video_project_id",
  dashboard: "custom_dashboard_id",
  fleet: "fleet_id",
};

/**
 * A studio rail's routes: its picker lists the agents carrying the studio's
 * tool (`?studio=`), a conversation it starts is bound to its subject
 * (`?presentation_id=` / `?video_project_id=` / `?custom_dashboard_id=` /
 * `?fleet_id=` on the session `POST`), and the
 * rail keeps its own list of the subject's conversations, so the panel's
 * history, workspaces and "Clear all" are off.
 */
export function studioPanelEndpoints(studio: StudioKind, subjectId: string): ApiEndpoints {
  return {
    ...CHAT_PANEL_ENDPOINTS,
    agents: `${CHAT_PANEL_ENDPOINTS.agents}?studio=${studio}`,
    sessions: `${CHAT_PANEL_ENDPOINTS.sessions}?${STUDIO_BINDING[studio]}=${encodeURIComponent(subjectId)}`,
    history: null,
    clearAll: null,
    workspaces: null,
  };
}

/**
 * The agent detail's read-only view of one of its conversations
 * (`agent_chats.py`): the restore and the older pages under the access rule
 * of that page, the file and artifact routes under their own, read aloud; no
 * agent list, no history, nothing that sends or follows a turn. With no agent
 * list the panel draws the agent as a static label (no chevron, no switcher),
 * never a one-entry or dead-end menu (xtm-one #10910, UX retest of #10911, F2):
 * the page hands it the agent's own mark (`logoIcon`).
 */
export function agentChatPanelEndpoints(agentId: string): ApiEndpoints {
  const base = `/agents/${encodeURIComponent(agentId)}/chats`;
  // Every route the chat page names, off, so none falls back to the package's
  // default: the view turns back on only those it reads. `messages` cannot be
  // off; a read-only view never sends.
  const off = Object.fromEntries(
    Object.keys(CHAT_PANEL_ENDPOINTS)
      .filter((key) => key !== "messages")
      .map((key) => [key, null]),
  ) as ApiEndpoints;
  return {
    ...off,
    sessions: `${base}/sessions`,
    messagesPage: `${base}/conversations`,
    download: CHAT_FILES_ROUTE,
    artifacts: CHAT_PANEL_ENDPOINTS.artifacts,
    tts: CHAT_PANEL_ENDPOINTS.tts,
  };
}

/**
 * The agent picker's entries that are not agents (`GET /web/chat/agents`,
 * mirrors `GENERAL_ASSISTANT_ENTRY` / `FLOW_ENTRY_PREFIX` in
 * `chat_web_stream.py`): the General Assistant, and a chat-flow by its id.
 */
export const GENERAL_ASSISTANT_ENTRY = "general-assistant:";
export const FLOW_ENTRY_PREFIX = "flow:";

/**
 * Where a chat file downloads, root-relative as the backend writes a stored
 * attachment's `download_url`: the sink (`authenticatedDownload`,
 * `useAuthenticatedSrc`) puts the platform's URL prefix on.
 */
export function chatFileDownloadPath(fileId: string): string {
  return `${stripBasePath(API_BASE)}${CHAT_FILES_ROUTE}/${encodeURIComponent(fileId)}/download`;
}
