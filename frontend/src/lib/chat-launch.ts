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
 * Open `/chat` with the composer already holding a message.
 *
 * `/chat` (`ChatPanelPage`) takes a new chat's draft from
 * `chatDraftStorageKey(null)`, and a conversation's from
 * `chatDraftStorageKey(<its id>)` when that conversation opens, putting it in
 * the composer; it reads + clears `CHAT_PENDING_AGENT_STORAGE_KEY` once on
 * mount to pre-select an agent, and `CHAT_AUTOSEND_STORAGE_KEY` to submit a new
 * chat's draft on arrival. This helper spells those writes once.
 *
 * Nothing is sent from here: the user reviews the message in the composer and
 * presses send themselves unless `autoSend` is set, which is reserved for
 * one-click actions whose wording the user has already seen on the button.
 */
import {
  CHAT_AUTOSEND_STORAGE_KEY,
  CHAT_DRAFT_STORAGE_KEY,
  CHAT_PENDING_AGENT_STORAGE_KEY,
} from "@/lib/constants";

/** Where a launcher leaves the composer's message for *conversationId* (`null`: a new chat). */
export function chatDraftStorageKey(conversationId: string | null): string {
  return `${CHAT_DRAFT_STORAGE_KEY}:${conversationId ?? "new"}`;
}

interface ChatDraftOptions {
  /** The existing conversation the draft is for (a run's follow-up chat).
   *  Omitted = the next new chat, which alone takes `agentId` and `autoSend`. */
  conversationId?: string;
  /** Agent id to pre-select, or `CHAT_GENERAL_AGENT_PARAM`. Omitted = let
   *  `/chat` resolve the user's default agent. */
  agentId?: string | null;
  /** Submit the draft as soon as the chat mounts. */
  autoSend?: boolean;
}

/**
 * Store the draft (and the optional agent hand-off) for the next `/chat` visit.
 * Best effort: a browser without sessionStorage still gets an empty chat.
 */
export function primeChatDraft(prompt: string, options: ChatDraftOptions = {}): void {
  try {
    if (options.conversationId) {
      sessionStorage.setItem(chatDraftStorageKey(options.conversationId), prompt.trim());
      return;
    }
    sessionStorage.setItem(chatDraftStorageKey(null), prompt.trim());
    if (options.agentId) {
      sessionStorage.setItem(CHAT_PENDING_AGENT_STORAGE_KEY, options.agentId);
    } else {
      sessionStorage.removeItem(CHAT_PENDING_AGENT_STORAGE_KEY);
    }
    if (options.autoSend) sessionStorage.setItem(CHAT_AUTOSEND_STORAGE_KEY, "1");
    else sessionStorage.removeItem(CHAT_AUTOSEND_STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode, quota) — the caller still navigates.
  }
}

/** What a launcher handed the next `/chat` visit: the agent, a new chat's draft, auto-send. */
export interface ChatLaunch {
  pendingAgent: string | null;
  draft: string;
  autoSend: boolean;
}

const NO_LAUNCH: ChatLaunch = { pendingAgent: null, draft: "", autoSend: false };

/**
 * Read, without clearing, what a launcher left for the next `/chat` visit.
 * With a conversation on the address only the agent hand-off is read: the
 * new chat's draft and auto-send wait for a new chat.
 *
 * Pure on purpose: React may call a render-time reader twice (StrictMode) or
 * throw its render away before commit, so a reader that also cleared would
 * leave the render that commits with nothing, and the page would resume the
 * last conversation instead. `clearChatLaunch` runs after commit.
 */
export function readChatLaunch(hasConversation: boolean): ChatLaunch {
  try {
    const pendingAgent = sessionStorage.getItem(CHAT_PENDING_AGENT_STORAGE_KEY);
    if (hasConversation) return { ...NO_LAUNCH, pendingAgent };
    return {
      pendingAgent,
      draft: sessionStorage.getItem(chatDraftStorageKey(null)) ?? "",
      autoSend: !!sessionStorage.getItem(CHAT_AUTOSEND_STORAGE_KEY),
    };
  } catch {
    return NO_LAUNCH;
  }
}

/**
 * Clear the hand-off *launch* was read from, so a reload never brings it back.
 * A key is removed only while it still holds what was read: a launch primed
 * after the read (another tab action, a second launcher) is kept for its visit.
 */
export function clearChatLaunch(launch: ChatLaunch, hasConversation: boolean): void {
  const removeIf = (key: string, read: string | null): boolean => {
    if (read === null || sessionStorage.getItem(key) !== read) return false;
    sessionStorage.removeItem(key);
    return true;
  };
  try {
    removeIf(CHAT_PENDING_AGENT_STORAGE_KEY, launch.pendingAgent);
    if (hasConversation) return;
    // The auto-send flag belongs to the draft it was primed with: it goes with that draft only.
    const draftCleared = removeIf(chatDraftStorageKey(null), launch.draft || null);
    if (launch.autoSend && (draftCleared || !launch.draft)) sessionStorage.removeItem(CHAT_AUTOSEND_STORAGE_KEY);
  } catch {
    // Storage unavailable: nothing was read from it either.
  }
}

/**
 * The draft a launcher left for *conversationId* (`null`: a new chat), cleared
 * as it is read so a reload never brings it back; `""` when there is none.
 */
export function takePrimedChatDraft(conversationId: string | null): string {
  try {
    const key = chatDraftStorageKey(conversationId);
    const draft = sessionStorage.getItem(key) ?? "";
    sessionStorage.removeItem(key);
    return draft;
  } catch {
    return "";
  }
}
