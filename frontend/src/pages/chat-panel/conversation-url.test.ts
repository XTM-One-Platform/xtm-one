// Every way the chat page opens or leaves a conversation (the panel, the
// "Shared with me" section, the resume) changes `?c=` and nothing else.

import { describe, expect, it } from "vitest";

import { withConversation } from "@/pages/chat-panel/conversation-url";

describe("withConversation", () => {
  it("sets the conversation and keeps every other parameter", () => {
    const next = withConversation(new URLSearchParams("arcade=snake&c=old&tab=files"), "new");
    expect(next.get("c")).toBe("new");
    expect(next.get("arcade")).toBe("snake");
    expect(next.get("tab")).toBe("files");
  });

  it("leaves the conversation for a new chat, keeping the rest", () => {
    const next = withConversation(new URLSearchParams("c=old&arcade=snake"), null);
    expect(next.has("c")).toBe(false);
    expect(next.get("arcade")).toBe("snake");
  });

  it("never changes the parameters it was given", () => {
    const current = new URLSearchParams("c=old");
    withConversation(current, "new");
    expect(current.get("c")).toBe("old");
  });
});
