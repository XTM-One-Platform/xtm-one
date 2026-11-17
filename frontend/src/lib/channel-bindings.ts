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

import type { QueryClient } from "@tanstack/react-query";

/**
 * Refresh what continuing a conversation in Slack or Teams, or stopping, changes:
 * every conversation's detail (it carries ``continued_in``) and channel
 * bindings, not only the conversation the dialog is open on - binding a Teams
 * personal chat takes it from the conversation it continued until then.
 */
export async function refreshChannelContinuation(qc: QueryClient): Promise<void> {
  await Promise.all([
    qc.invalidateQueries({ queryKey: ["conversation-channel-bindings"] }),
    qc.invalidateQueries({ queryKey: ["conversation"] }),
  ]);
}
