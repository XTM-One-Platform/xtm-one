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

import { useEffect, type RefObject } from "react";
import type { ChatPanelHandle } from "@filigran/chatbot";
import { takePrimedChatDraft } from "@/lib/chat-launch";

/**
 * Puts in the composer the draft a launcher primed for the conversation on
 * screen (`primeChatDraft(prompt, { conversationId })`: a run's follow-up
 * chat), once the panel is ready and each time a conversation opens. The draft
 * is taken as it is read, so a reload or a return to the conversation does not
 * bring it back.
 */
export function usePrimedConversationDraft(
  panelReady: boolean,
  conversationId: string | null,
  panelRef: RefObject<Pick<ChatPanelHandle, "setDraft" | "focusComposer"> | null>,
): void {
  useEffect(() => {
    if (!panelReady || !conversationId) return;
    const draft = takePrimedChatDraft(conversationId);
    if (!draft.trim()) return;
    panelRef.current?.setDraft(draft);
    panelRef.current?.focusComposer();
  }, [panelReady, conversationId, panelRef]);
}
