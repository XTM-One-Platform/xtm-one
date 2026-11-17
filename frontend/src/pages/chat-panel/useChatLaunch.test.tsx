// A launch primed for the next new chat (#11265) is read by every render of
// the chat page's first mount, StrictMode's double invoke and a render thrown
// away before commit included, and cleared once the page has committed: the
// page never loses it and resumes the last conversation instead.

import { StrictMode, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";

import { chatDraftStorageKey, clearChatLaunch, primeChatDraft, readChatLaunch } from "@/lib/chat-launch";
import { CHAT_AUTOSEND_STORAGE_KEY, CHAT_PENDING_AGENT_STORAGE_KEY } from "@/lib/constants";
import { useChatLaunch } from "@/pages/chat-panel/useChatLaunch";

const strict = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;

afterEach(() => sessionStorage.clear());

describe("readChatLaunch", () => {
  it("reads the same launch however many times it is called, and clears nothing", () => {
    primeChatDraft("Interview me", { agentId: "agent-1", autoSend: true });
    const first = readChatLaunch(false);
    expect(readChatLaunch(false)).toEqual(first);
    expect(first).toEqual({ pendingAgent: "agent-1", draft: "Interview me", autoSend: true });
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBe("Interview me");
  });

  it("reads only the agent when a conversation is on the address", () => {
    primeChatDraft("Interview me", { agentId: "agent-1", autoSend: true });
    expect(readChatLaunch(true)).toEqual({ pendingAgent: "agent-1", draft: "", autoSend: false });
  });
});

describe("clearChatLaunch", () => {
  it("keeps a launch primed after the read", () => {
    primeChatDraft("First", { autoSend: true });
    const read = readChatLaunch(false);
    primeChatDraft("Second", { agentId: "agent-2", autoSend: true });
    clearChatLaunch(read, false);
    expect(readChatLaunch(false)).toEqual({ pendingAgent: "agent-2", draft: "Second", autoSend: true });
  });

  it("leaves a new chat's draft for a new chat when a conversation was opened", () => {
    primeChatDraft("Later", { agentId: "agent-1" });
    clearChatLaunch(readChatLaunch(true), true);
    expect(sessionStorage.getItem(CHAT_PENDING_AGENT_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBe("Later");
  });
});

describe("useChatLaunch", () => {
  it("returns the primed launch under StrictMode's double invoke, then clears it", () => {
    primeChatDraft("Interview me", { autoSend: true });
    const { result, rerender } = renderHook(() => useChatLaunch(false), { wrapper: strict });
    expect(result.current).toEqual({ pendingAgent: null, draft: "Interview me", autoSend: true });
    rerender();
    expect(result.current.draft).toBe("Interview me");
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBeNull();
    expect(sessionStorage.getItem(CHAT_AUTOSEND_STORAGE_KEY)).toBeNull();
  });

  it("survives a first render thrown away before commit", () => {
    primeChatDraft("Interview me", { autoSend: true });
    // The discarded render reads and never commits: nothing may be cleared by it.
    readChatLaunch(false);
    const { result } = renderHook(() => useChatLaunch(false));
    expect(result.current).toEqual({ pendingAgent: null, draft: "Interview me", autoSend: true });
  });

  it("is consumed once: the next visit finds no launch", () => {
    primeChatDraft("Interview me", { autoSend: true });
    const first = renderHook(() => useChatLaunch(false));
    first.unmount();
    const { result } = renderHook(() => useChatLaunch(false));
    expect(result.current).toEqual({ pendingAgent: null, draft: "", autoSend: false });
  });
});
