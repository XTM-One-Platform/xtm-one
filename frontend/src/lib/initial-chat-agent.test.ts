// The agent a new `/chat` starts with (`initialChatAgent`): an explicit
// `?agent=` and a launcher's hand-off open an agent built for assignments too,
// as the agent page's "Chat with Agent" offers; the default skips it.
import { describe, expect, it } from "vitest";

import { CHAT_GENERAL_AGENT_PARAM } from "@/lib/constants";
import { initialChatAgent } from "@/lib/utils";
import type { Agent, User } from "@/types";

const agent = (id: string, overrides: Partial<Agent> = {}) =>
  ({ id, slug: id, enabled: true, disable_chat: false, is_company_managed: true, ...overrides }) as Agent;

const chat = agent("chat");
const assignments = agent("assignments", { disable_chat: true });
const off = agent("off", { enabled: false });
const agents = [chat, assignments, off];
const user = { default_agent_id: "chat" } as User;

const start = (agentParam: string | null, pendingAgent: string | null = null) =>
  initialChatAgent({ agentParam, pendingAgent }, user, agents, true, null)?.id ?? null;

describe("the agent a new chat starts with", () => {
  it("is the agent `?agent=` names, one built for assignments included", () => {
    expect(start("assignments")).toBe("assignments");
    expect(start("chat")).toBe("chat");
  });

  it("is never a disabled or unknown agent: the user's default stands in", () => {
    expect(start("off")).toBe("chat");
    expect(start("nobody")).toBe("chat");
  });

  it("takes the hand-off's agent, one built for assignments included (#774)", () => {
    expect(start(null, "assignments")).toBe("assignments");
    expect(start(null, "off")).toBe("chat");
    expect(initialChatAgent({ agentParam: null, pendingAgent: CHAT_GENERAL_AGENT_PARAM }, user, agents, true, null)).toBeNull();
  });
});
