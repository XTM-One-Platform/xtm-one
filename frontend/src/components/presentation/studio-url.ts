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
 * The Presentation Studio's place in the viewer URL.
 *
 * `/presentations/:id?studio=1` opens the viewer with the studio rail out;
 * `&c=<conversation>` names the rail's conversation, the way `/chat?c=` does.
 * Every entry into the studio — the toolbar toggle, "Edit with my assistant"
 * on a library card, "Open in studio" on a chat card — builds its link here so
 * the parameters cannot drift between them, and a reload of the viewer lands
 * back in the same rail on the same conversation.
 */

export const STUDIO_PARAM = "studio";
export const STUDIO_CONVERSATION_PARAM = "c";

export function studioUrl(presentationId: string, conversationId?: string | null): string {
  const params = new URLSearchParams({ [STUDIO_PARAM]: "1" });
  if (conversationId) params.set(STUDIO_CONVERSATION_PARAM, conversationId);
  return `/presentations/${presentationId}?${params.toString()}`;
}
