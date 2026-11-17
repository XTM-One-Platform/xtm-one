// A draft primed for a conversation (a run's follow-up chat) lands in the
// composer when that conversation opens on a ready panel, once: the key is
// taken as it is read. A new chat's draft and another conversation's stay put.

import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { chatDraftStorageKey, primeChatDraft, takePrimedChatDraft } from "@/lib/chat-launch";
import { CHAT_AUTOSEND_STORAGE_KEY, CHAT_PENDING_AGENT_STORAGE_KEY } from "@/lib/constants";
import { usePrimedConversationDraft } from "@/pages/chat-panel/usePrimedConversationDraft";

const CID = "11111111-2222-4333-8444-555555555555";
const OTHER = "99999999-2222-4333-8444-555555555555";

function panel() {
  return { current: { setDraft: vi.fn(), focusComposer: vi.fn() } };
}

afterEach(() => sessionStorage.clear());

describe("primeChatDraft for a conversation", () => {
  it("keeps the draft under that conversation's key, and touches no new-chat hand-off", () => {
    sessionStorage.setItem(CHAT_PENDING_AGENT_STORAGE_KEY, "agent-1");
    sessionStorage.setItem(CHAT_AUTOSEND_STORAGE_KEY, "1");
    primeChatDraft("  And the next step?  ", { conversationId: CID, autoSend: true });
    expect(sessionStorage.getItem(chatDraftStorageKey(CID))).toBe("And the next step?");
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBeNull();
    expect(sessionStorage.getItem(CHAT_PENDING_AGENT_STORAGE_KEY)).toBe("agent-1");
    expect(sessionStorage.getItem(CHAT_AUTOSEND_STORAGE_KEY)).toBe("1");
  });

  it("is taken once", () => {
    primeChatDraft("Go on", { conversationId: CID });
    expect(takePrimedChatDraft(CID)).toBe("Go on");
    expect(takePrimedChatDraft(CID)).toBe("");
  });
});

describe("usePrimedConversationDraft", () => {
  it("puts the conversation's primed draft in the composer once the panel is ready, and clears it", () => {
    primeChatDraft("Follow up on the run", { conversationId: CID });
    primeChatDraft("A new chat's message");
    const ref = panel();
    const hook = renderHook(({ ready }) => usePrimedConversationDraft(ready, CID, ref), {
      initialProps: { ready: false },
    });
    expect(ref.current.setDraft).not.toHaveBeenCalled();
    hook.rerender({ ready: true });
    expect(ref.current.setDraft).toHaveBeenCalledWith("Follow up on the run");
    expect(ref.current.focusComposer).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(chatDraftStorageKey(CID))).toBeNull();
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBe("A new chat's message");
  });

  it("reads the draft of the conversation that opens, and does nothing without one", () => {
    primeChatDraft("For the other one", { conversationId: OTHER });
    const ref = panel();
    const hook = renderHook(({ id }) => usePrimedConversationDraft(true, id, ref), {
      initialProps: { id: CID as string | null },
    });
    expect(ref.current.setDraft).not.toHaveBeenCalled();
    hook.rerender({ id: OTHER });
    expect(ref.current.setDraft).toHaveBeenCalledWith("For the other one");
    hook.rerender({ id: null });
    hook.rerender({ id: OTHER });
    expect(ref.current.setDraft).toHaveBeenCalledTimes(1);
  });
});
