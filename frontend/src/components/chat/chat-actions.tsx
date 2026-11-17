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
 * What a block inside the chat transcript may ask of the chat around it.
 *
 * A block (a Deep Investigation) can hand work to the conversation's
 * agent - "Generate executive summary" - without knowing how the page sends a
 * message. The chat page provides it; a block rendered anywhere else (an
 * agent's chats tab, a read-only transcript) finds no provider and offers no
 * such action.
 */
import { createContext, useContext } from "react";

export interface ChatActions {
  /**
   * Send `prompt` to the conversation's agent as the user's next message, as
   * an ordinary turn (never a Deep Investigation) and without touching the
   * composer, with `builtinToolIds` and `integrationIds` added to THIS turn's
   * selection so the agent can do what it is asked (the server keeps only
   * what the user may add, and does not save the one-off selection).
   */
  sendPrompt: (prompt: string, options?: { builtinToolIds?: string[]; integrationIds?: string[] }) => void;
  /** Whether a send would be accepted right now: licence, AI switch, a turn
   *  in flight, a conversation the user may write to, its tools resolved. */
  canSend: boolean;
  /** True while the only thing keeping `canSend` false may be the
   *  conversation's tools still resolving, so a block can say so. */
  toolsResolving?: boolean;
}

export const ChatActionsContext = createContext<ChatActions | null>(null);

export function useChatActions(): ChatActions | null {
  return useContext(ChatActionsContext);
}
