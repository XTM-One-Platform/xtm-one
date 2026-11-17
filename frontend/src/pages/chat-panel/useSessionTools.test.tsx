// The session tools of the chat page: started from the agent's defaults (or
// the General Assistant's), restored from a conversation's saved overrides,
// kept across the first send of a new chat, and sent with each message.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import type { DefaultToolSets } from "@/pages/chat/session-tools";
import { useSessionTools } from "@/pages/chat-panel/useSessionTools";
import { testQueryClient, withProviders } from "@/testing/providers";
import type { ConversationDetail } from "@/types";

const AGENT_DEFAULTS: DefaultToolSets = { builtin: ["web"], custom: ["ct-1"], integration: ["int-1"], mcp: ["mcp-1"], kb: [] };
const GA_DEFAULTS: DefaultToolSets = { builtin: ["files"], custom: [], integration: [], mcp: [], kb: ["kb-1"] };

const data = vi.hoisted(() => ({
  agents: [{ id: "agent-1", name: "Analyst" }],
  integrations: [
    { id: "int-1", provider: "opencti", status: "connected", scope_config: null },
    { id: "int-2", provider: "jira", status: "connected", scope_config: { match_key: "x", resolve_from_header: "X-Jira-Site" } },
  ],
  allKbs: [{ id: "kb-ws", managed_by_workspace: "ws-1" }, { id: "kb-2" }],
  bindableKbs: [{ id: "kb-2" }],
  builtinToolsReady: true,
}));

vi.mock("@/hooks/api/useAgentApi", () => ({ useAgents: () => ({ data: data.agents }) }));
vi.mock("@/hooks/api/useAuthApi", () => ({ useMe: () => ({ data: { id: "u-1" } }) }));
vi.mock("@/hooks/api/useCustomToolApi", () => ({ useCustomTools: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useIntegrationApi", () => ({ useIntegrations: () => ({ data: data.integrations }) }));
vi.mock("@/hooks/api/useKnowledgeBaseApi", () => ({
  // A workspace's own knowledge base is in the full list, never in the bindable one.
  useKnowledgeBases: () => ({ data: data.allKbs }),
  useBindableKnowledgeBases: () => ({ data: data.bindableKbs }),
}));
vi.mock("@/hooks/api/useMCPServerApi", () => ({ useMCPServers: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useSettingsApi", () => ({ useGeneralAssistantConfig: () => ({ data: {} }) }));
vi.mock("@/hooks/useBuiltinTools", () => ({
  useBuiltinTools: () => ({
    offeredInChat: () => [{ id: "web" }, { id: "files" }],
    canonicalIds: (ids: readonly string[] | null | undefined) => [...(ids ?? [])],
    isReady: data.builtinToolsReady,
  }),
}));
vi.mock("@/pages/chat/session-tools", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/chat/session-tools")>()),
  agentDefaultToolSets: () => AGENT_DEFAULTS,
  gaDefaultToolSets: () => GA_DEFAULTS,
}));

type Props = { agentId: string | null; conversation: ConversationDetail | null | undefined; createdHere: (id: string) => boolean };

function setup(initial: Partial<Props> = {}) {
  const props: Props = { agentId: "agent-1", conversation: null, createdHere: () => false, ...initial };
  return renderHook((p: Props) => useSessionTools(p), { wrapper: withProviders(testQueryClient()), initialProps: props });
}

const conversation = (overrides: Partial<ConversationDetail>): ConversationDetail =>
  ({ id: "c-1", agent_id: "agent-1", messages: [], tool_overrides: null, ...overrides }) as ConversationDetail;

beforeEach(() => {
  localStorage.clear();
  data.builtinToolsReady = true;
  data.agents = [{ id: "agent-1", name: "Analyst" }];
});

describe("useSessionTools", () => {
  it("starts a new chat from the agent's defaults, and sends them with the defaults they were made against", () => {
    const { result } = setup();
    expect(result.current.initialized).toBe(true);
    expect(result.current.toolCount).toBe(4);
    expect(result.current.requestFields("agent-1")).toEqual({
      builtin_tool_ids: ["web"],
      custom_tool_ids: ["ct-1"],
      integration_ids: ["int-1"],
      mcp_server_ids: ["mcp-1"],
      knowledge_base_ids: [],
      default_tool_ids: ["web", "ct-1", "int-1", "mcp-1"],
    });
  });

  it("starts the General Assistant from its own defaults", () => {
    const { result } = setup({ agentId: null });
    expect(result.current.requestFields(null).builtin_tool_ids).toEqual(["files"]);
    expect(result.current.requestFields(null).knowledge_base_ids).toEqual(["kb-1"]);
  });

  it("restores a conversation's saved overrides, plus a default added to the agent since", () => {
    const { result } = setup({
      conversation: conversation({
        tool_overrides: {
          builtin_tool_ids: [],
          custom_tool_ids: ["ct-1"],
          integration_ids: [],
          mcp_server_ids: [],
          knowledge_base_ids: [],
          // `web` was a default then and switched off here; `mcp-1` is new.
          default_tool_ids: ["web", "ct-1", "int-1"],
        },
      } as Partial<ConversationDetail>),
    });
    const fields = result.current.requestFields("agent-1");
    expect(fields.builtin_tool_ids).toEqual([]);
    expect(fields.integration_ids).toEqual([]);
    expect(fields.custom_tool_ids).toEqual(["ct-1"]);
    expect(fields.mcp_server_ids).toEqual(["mcp-1"]);
  });

  it("keeps what was picked on a new chat once its first send creates the conversation", () => {
    const { result, rerender } = setup();
    act(() => result.current.pickerProps.onToggleBuiltin("web"));
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual([]);
    rerender({ agentId: "agent-1", conversation: conversation({}), createdHere: (id) => id === "c-1" });
    expect(result.current.initialized).toBe(true);
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual([]);
  });

  it("starts again from the new agent's tools when the conversation is continued with another agent", () => {
    const { result, rerender } = setup({ conversation: conversation({}) });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual(["web"]);
    // Continued with the General Assistant: the page's agent is the conversation's.
    rerender({ agentId: null, conversation: conversation({ agent_id: null }), createdHere: () => false });
    expect(result.current.requestFields(null).builtin_tool_ids).toEqual(["files"]);
    expect(result.current.requestFields(null).knowledge_base_ids).toEqual(["kb-1"]);
  });

  it("restores a conversation created here from its saved tools once the page left its draft", () => {
    const { result, rerender } = setup();
    act(() => result.current.pickerProps.onToggleBuiltin("web"));
    rerender({ agentId: "agent-1", conversation: conversation({}), createdHere: (id) => id === "c-1" });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual([]);
    // Continued with another agent: the draft's picks were the first agent's.
    rerender({ agentId: null, conversation: conversation({ agent_id: null }), createdHere: (id) => id === "c-1" });
    expect(result.current.requestFields(null).builtin_tool_ids).toEqual(["files"]);
  });

  const webOff = {
    tool_overrides: {
      builtin_tool_ids: [],
      custom_tool_ids: ["ct-1"],
      integration_ids: ["int-1"],
      mcp_server_ids: ["mcp-1"],
      knowledge_base_ids: [],
      default_tool_ids: ["web", "ct-1", "int-1", "mcp-1"],
    },
  } as Partial<ConversationDetail>;

  it("starts again from the saved tools a conversation is read back with, when its cached copy predated them", () => {
    // Opened from a copy cached before its last send saved the tools: the defaults first.
    const { result, rerender } = setup({ conversation: conversation({}) });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual(["web"]);
    rerender({ agentId: "agent-1", conversation: conversation(webOff), createdHere: () => false });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual([]);
  });

  it("keeps a choice made before the conversation is read back, whatever it saved", () => {
    const { result, rerender } = setup({ conversation: conversation({}) });
    act(() => result.current.pickerProps.onToggleBuiltin("files"));
    rerender({ agentId: "agent-1", conversation: conversation(webOff), createdHere: () => false });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual(["web", "files"]);
  });

  it("switches everything off, back to the defaults, and on", () => {
    const { result } = setup();
    act(() => result.current.pickerProps.onDisableAll());
    expect(result.current.toolCount).toBe(0);
    act(() => result.current.pickerProps.onResetDefault());
    expect(result.current.toolCount).toBe(4);
    act(() => result.current.pickerProps.onEnableAll());
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual(["web", "files"]);
    expect(result.current.requestFields("agent-1").integration_ids).toEqual(["int-1", "int-2"]);
    // Never a workspace's own knowledge base, which no binding route takes.
    expect(result.current.requestFields("agent-1").knowledge_base_ids).toEqual(["kb-2"]);
  });

  it("enables nothing before the built-in tools registry is known", () => {
    data.builtinToolsReady = false;
    const { result } = setup();
    act(() => result.current.pickerProps.onDisableAll());
    act(() => result.current.pickerProps.onEnableAll());
    expect(result.current.toolCount).toBe(0);
  });

  it("sends no selection before it is started for the conversation, never empty lists", () => {
    data.agents = [];
    const { result, rerender } = setup({ conversation: conversation({}) });
    expect(result.current.requestFields("agent-1")).toEqual({});
    data.agents = [{ id: "agent-1", name: "Analyst" }];
    rerender({ agentId: "agent-1", conversation: conversation({}), createdHere: () => false });
    expect(result.current.requestFields("agent-1").builtin_tool_ids).toEqual(["web"]);
  });

  it("hides an integration for good, and brings it back", () => {
    const { result } = setup();
    act(() => result.current.pickerProps.onHideIntegration("int-1"));
    expect(result.current.requestFields("agent-1").integration_ids).toEqual([]);
    expect(JSON.parse(localStorage.getItem("copilot:hidden-integrations") ?? "[]")).toEqual(["int-1"]);
    act(() => result.current.pickerProps.onUnhideIntegration("int-1"));
    expect(result.current.requestFields("agent-1").integration_ids).toEqual(["int-1"]);
    expect(JSON.parse(localStorage.getItem("copilot:hidden-integrations") ?? "[]")).toEqual([]);
  });

  it("sends the platform picked for a scoped provider as the header it resolves from", () => {
    const { result } = setup();
    act(() => result.current.pickerProps.onScopeChange("jira", "int-2"));
    const fields = result.current.requestFields("agent-1");
    expect(fields.integration_ids).toContain("int-2");
    expect(fields.scope_headers).toEqual({ "x-jira-site": "int-2" });
  });
});
