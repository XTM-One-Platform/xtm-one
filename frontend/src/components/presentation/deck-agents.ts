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
 * Which assistants can write a deck: those carrying the
 * `generate_presentation` built-in tool (the `Presentation Generation` entry
 * of an agent's tools), or the General Assistant when its effective tools do.
 * The Presentation Studio rail (which agent edits THIS deck) and the "New
 * presentation" dialog (which agent writes the next one) read the same answer,
 * `useToolAgents` — the one the Video Studio reads for its own tool.
 */
import { type ToolAgents, useToolAgents } from "@/hooks/useToolAgents";

/** The built-in tool an agent must carry to be offered as a deck writer.
 * @public
 */
export const DECK_TOOL_ID = "generate_presentation";

type DeckAgents = ToolAgents;

export function useDeckAgents(): DeckAgents {
  return useToolAgents(DECK_TOOL_ID);
}
