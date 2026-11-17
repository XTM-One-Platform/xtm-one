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
 * The web chat's routes for the task pane: a copy of CHAT_PANEL_ENDPOINTS
 * (frontend/src/components/chat-panel/endpoints.ts), which the add-in cannot
 * import; office-addin-panel-config.test.ts holds them equal.
 *
 * Pure and import-free: the frontend's unit tests read it
 * (`frontend/tests_unit/office-addin-panel-config.test.ts`) without the
 * add-in's dependencies; the panel takes it as its `ApiEndpoints`.
 */

/** The base the routes below are relative to, on the platform the pane signed in to. */
export function panelApiBaseUrl(platformUrl: string): string {
  return `${platformUrl.replace(/\/+$/, "")}/api/v1`;
}

export const OFFICE_PANEL_ENDPOINTS = {
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
  download: "/chat/files",
  tts: "/chat/tts",
  // As on the chat page: the composer's agentic quota indicator (today by default).
  quota: "/web/chat/quota",
} as const;

/** Namespaces the panel's browser storage (conversation on screen, drafts). */
export const OFFICE_PANEL_STORAGE_PREFIX = "filigran_copilot";

/** The platform's upload limit (`MAX_UPLOAD_SIZE_BYTES`): the composer never uploads a file the route would refuse. */
export const OFFICE_PANEL_MAX_UPLOAD_BYTES = 40 * 1024 * 1024;

/**
 * A user's message as the pane drew it before the panel: the dimmed accent behind the pane's own text, not white on the accent.
 * The composer box takes the design system's small radius (4px), as the web chat's `PANEL_THEME.inputRadius`
 * (`var(--radius-sm)` there); the pane's own `--radius-sm` is 6px, so the value is written out (xtm-one #10910, section E).
 * The panel's secondary ink (the quota count, its periods panel) is the pane's `--text-secondary`, as the web chat's
 * `PANEL_THEME.textMuted` is its secondary text: unset, the panel falls back to a gray under 4.5:1 on the light pane.
 */
export const OFFICE_PANEL_THEME = {
  userBackground: "var(--cyan-dim)",
  userText: "var(--text-primary)",
  textMuted: "var(--text-secondary)",
  inputRadius: "4px",
};

/**
 * The web chat's wording for the panel's own strings the pane shows, keyed as the panel asks for them:
 * a copy of the English catalog's entries (`frontend/src/i18n/lang/en.json`), which the add-in cannot
 * import; office-addin-panel-config.test.ts holds them equal.
 */
export const OFFICE_PANEL_STRINGS: Readonly<Record<string, string>> = {
  "Uses AI. Verify results.": "AI can make mistakes. Check important information.",
};

/** The panel's `t`: the web chat's wording for a key above, any other key unchanged (the panel's own catalog). */
export function officePanelTranslate(key: string): string {
  return OFFICE_PANEL_STRINGS[key] ?? key;
}

/** Most files on one message, as the web chat's `MAX_CHAT_ATTACHMENTS_PER_MESSAGE` (the panel's own default is 10). */
export const OFFICE_PANEL_MAX_FILES_PER_MESSAGE = 30;

/** The composer's file limits, as the web chat's: each file up to the platform's limit, no cap on their total, 30 files a message. */
export const OFFICE_PANEL_FILE_LIMITS = {
  maxFileSize: OFFICE_PANEL_MAX_UPLOAD_BYTES,
  maxTotalSize: Number.POSITIVE_INFINITY,
  maxFileCount: OFFICE_PANEL_MAX_FILES_PER_MESSAGE,
};
