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
 * Hand a question to the assistant: put it in a new conversation's composer,
 * for the person to read and send, and open the chat.
 *
 * Never sent on its own. The prompt may have been written by an agent, from
 * data it read (a web page, a ticket, a tool result): sending it as the
 * person's message would let that data act with the person's authority. So
 * it only prefills a NEW conversation's composer, through the draft key
 * `Chat` restores on arrival.
 */
import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { CHAT_AUTOSEND_STORAGE_KEY, CHAT_DRAFT_STORAGE_KEY } from "@/lib/constants";

/** The draft a fresh `/chat` restores (`getChatDraftStorageKey(null)` in `Chat.tsx`). */
const NEW_CHAT_DRAFT_KEY = `${CHAT_DRAFT_STORAGE_KEY}:new`;

/** Put `prompt` (none: an empty composer) in a new conversation's composer. */
export type AskAssistant = (prompt?: string) => void;

/** Prefill a new chat's composer and open it. */
export function useAskAssistant(): AskAssistant {
  const navigate = useNavigate();
  return useCallback(
    (prompt?: string) => {
      try {
        const trimmed = prompt?.trim();
        if (trimmed) sessionStorage.setItem(NEW_CHAT_DRAFT_KEY, trimmed);
        else sessionStorage.removeItem(NEW_CHAT_DRAFT_KEY);
        // A draft left armed by another page must not send this one.
        sessionStorage.removeItem(CHAT_AUTOSEND_STORAGE_KEY);
      } catch {
        // Storage unavailable: the chat still opens, with an empty composer.
      }
      navigate("/chat");
    },
    [navigate],
  );
}
