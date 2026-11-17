// A chat file's download path is root-relative, as the backend writes a
// stored attachment's `download_url`: the sink puts the platform's URL prefix
// on, so a page served under one never gets it twice.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiEndpoints } from "@filigran/chatbot";

function servedUnder(prefix: string | null) {
  document.head.querySelector('meta[name="base-path"]')?.remove();
  if (prefix === null) return;
  const meta = document.createElement("meta");
  meta.name = "base-path";
  meta.content = prefix;
  document.head.appendChild(meta);
}

afterEach(() => {
  servedUnder(null);
  vi.resetModules();
});

describe("chatFileDownloadPath", () => {
  it("is the native chat's file route at the root", async () => {
    servedUnder(null);
    vi.resetModules();
    const { chatFileDownloadPath } = await import("@/components/chat-panel/endpoints");
    expect(chatFileDownloadPath("f-1")).toBe("/api/v1/chat/files/f-1/download");
  });

  it("stays root-relative under a URL prefix, which the download adds once", async () => {
    servedUnder("/xtm-one");
    vi.resetModules();
    const { chatFileDownloadPath } = await import("@/components/chat-panel/endpoints");
    const { withBasePath } = await import("@/lib/base-path");
    const path = chatFileDownloadPath("f 1");
    expect(path).toBe("/api/v1/chat/files/f%201/download");
    expect(withBasePath(path)).toBe("/xtm-one/api/v1/chat/files/f%201/download");
  });
});

describe("studioPanelEndpoints", () => {
  it("lists the studio's agents, binds a new session to the subject and keeps the rail's own history", async () => {
    const { CHAT_PANEL_ENDPOINTS, studioPanelEndpoints } = await import("@/components/chat-panel/endpoints");
    const deck = studioPanelEndpoints("presentation", "d 1");
    expect(deck.agents).toBe("/web/chat/agents?studio=presentation");
    expect(deck.sessions).toBe("/web/chat/sessions?presentation_id=d%201");
    expect(studioPanelEndpoints("video", "v1").sessions).toBe("/web/chat/sessions?video_project_id=v1");
    expect([deck.history, deck.clearAll, deck.workspaces]).toEqual([null, null, null]);
    // Older pages, turns and their controls are the web chat's own.
    for (const key of ["messages", "steer", "approve", "cancel", "skipWait", "events", "messagesPage", "upload"] as const) {
      expect(deck[key]).toBe(CHAT_PANEL_ENDPOINTS[key]);
    }
  });
});

describe("CHAT_PANEL_ENDPOINTS.quota", () => {
  it("serves the web chat's quota route, so the panel draws the one quota indicator", async () => {
    const { CHAT_PANEL_ENDPOINTS, studioPanelEndpoints, agentChatPanelEndpoints } = await import("@/components/chat-panel/endpoints");
    expect(CHAT_PANEL_ENDPOINTS.quota).toBe("/web/chat/quota");
    // A studio rail sends turns: it shows the quota too. A read-only view does not.
    expect(studioPanelEndpoints("presentation", "d1").quota).toBe("/web/chat/quota");
    expect(agentChatPanelEndpoints("a1").quota).toBeNull();
  });
});

describe("agentChatPanelEndpoints", () => {
  it("restores and pages through the agent detail's routes and names nothing that sends", async () => {
    const { agentChatPanelEndpoints, CHAT_PANEL_ENDPOINTS } = await import("@/components/chat-panel/endpoints");
    const routes = agentChatPanelEndpoints("a1");
    expect(routes.sessions).toBe("/agents/a1/chats/sessions");
    expect(routes.messagesPage).toBe("/agents/a1/chats/conversations");
    // Off, not left out: a route left out would be the package's default.
    // No agent list: the panel draws the agent as a static label, never a
    // one-entry switcher or a dead-end menu (#10910, UX retest of #10911, F2).
    expect(routes.agents).toBeNull();
    const used = new Set(["sessions", "messagesPage", "download", "artifacts", "tts", "messages"]);
    for (const key of Object.keys(CHAT_PANEL_ENDPOINTS) as (keyof ApiEndpoints)[]) {
      if (!used.has(key)) expect(routes[key], `${key} is not off`).toBeNull();
    }
  });
});
