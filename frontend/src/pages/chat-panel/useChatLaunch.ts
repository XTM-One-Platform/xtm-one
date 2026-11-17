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

import { useEffect, useState } from "react";
import { clearChatLaunch, readChatLaunch, type ChatLaunch } from "@/lib/chat-launch";

/**
 * What a launcher handed this `/chat` visit (`primeChatDraft`: a new chat's
 * draft, the agent, auto-send), consumed once.
 *
 * Read during the first render, cleared only after it commits (#11265): a
 * render React runs twice (StrictMode) or throws away before commit (a child
 * suspending on the first mount) reads the same launch again instead of
 * finding the keys gone, so the page never resumes the last conversation in
 * place of the chat it was asked to open.
 */
export function useChatLaunch(hasConversation: boolean): ChatLaunch {
  const [read] = useState(() => ({ launch: readChatLaunch(hasConversation), hasConversation }));
  useEffect(() => {
    clearChatLaunch(read.launch, read.hasConversation);
  }, [read]);
  return read.launch;
}
