import { afterEach, expect, test, vi } from "vitest";

import { agentPageUrl, openAgentPage } from "./agentPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("an agent's page is /agents/<id> on the platform the pane signed in to", () => {
  expect(agentPageUrl("https://xtm.test/", "a1")).toBe("https://xtm.test/agents/a1");
  expect(agentPageUrl("https://xtm.test/one", "a b")).toBe("https://xtm.test/one/agents/a%20b");
});

test("the General Assistant and a chat-flow have no page, as on the web chat", () => {
  expect(agentPageUrl("https://xtm.test", "general-assistant:")).toBeUndefined();
  expect(agentPageUrl("https://xtm.test", "flow:f1")).toBeUndefined();
  expect(agentPageUrl("https://xtm.test", "")).toBeUndefined();
});

test("the page opens in Office's browser window when the host offers one", () => {
  const openBrowserWindow = vi.fn();
  const open = vi.fn();
  vi.stubGlobal("Office", { context: { ui: { openBrowserWindow } } });
  vi.stubGlobal("open", open);
  openAgentPage("https://xtm.test/agents/a1");
  expect(openBrowserWindow).toHaveBeenCalledWith("https://xtm.test/agents/a1");
  expect(open).not.toHaveBeenCalled();
});

test("without Office's browser window the page opens in a new tab", () => {
  const open = vi.fn();
  vi.stubGlobal("Office", { context: {} });
  vi.stubGlobal("open", open);
  openAgentPage("https://xtm.test/agents/a1");
  expect(open).toHaveBeenCalledWith("https://xtm.test/agents/a1", "_blank", "noopener,noreferrer");
});
