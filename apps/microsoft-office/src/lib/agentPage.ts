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
 * The agent's page on the platform, for the switcher's "Open agent page" item.
 *
 * The web chat links `/agents/<id>` under its own base path
 * (`agentPageHref`, frontend/src/components/chat-panel/panel-host.ts). The
 * task pane has no agent route of its own: the page is the platform's, an
 * absolute URL on the origin the pane signed in to, opened in a new window so
 * the pane keeps its conversation.
 *
 * Pure and import-free, as panelConfig.ts.
 */

/** The picker entries that are not an agent (frontend/src/components/chat-panel/endpoints.ts). */
const GENERAL_ASSISTANT_ENTRY = "general-assistant:";
const FLOW_ENTRY_PREFIX = "flow:";

/** `<platform>/agents/<id>`, or `undefined` for the General Assistant and a chat-flow, which have no page. */
export function agentPageUrl(platformUrl: string, agentId: string): string | undefined {
  if (!agentId || agentId === GENERAL_ASSISTANT_ENTRY || agentId.startsWith(FLOW_ENTRY_PREFIX)) return undefined;
  return `${platformUrl.replace(/\/+$/, "")}/agents/${encodeURIComponent(agentId)}`;
}

interface OfficeUi {
  openBrowserWindow?: (url: string) => void;
}

/**
 * Opens *url* outside the pane: Office's own browser window when the host
 * offers one (OpenBrowserWindowApi), else a new browser tab.
 */
export function openAgentPage(url: string): void {
  const office = (globalThis as { Office?: { context?: { ui?: OfficeUi } } }).Office;
  const ui = office?.context?.ui;
  if (ui && typeof ui.openBrowserWindow === "function") {
    ui.openBrowserWindow(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
