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

import type { ChatConversationChange } from "@filigran/chatbot";

/**
 * The conversation that keeps the choices made on a new chat's draft (its session
 * tools, its machine switch): the one the draft's first send created, while it is on
 * screen. Any other change ends it, a new chat or another conversation opened, so a
 * conversation created on the page and opened again later reads its own saved choices.
 */
export function createdFromDraft(id: string | null, change: ChatConversationChange): string | null {
  return id && change.created ? id : null;
}
