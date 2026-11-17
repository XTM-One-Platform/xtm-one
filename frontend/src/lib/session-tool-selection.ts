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
 * The tool selection a chat send carries.
 *
 * The server reads an explicit list as the turn's selection, `[]` included
 * ("no tools", honoured and saved as the conversation's), and an omitted list
 * as "the agent's (or the General Assistant's) own tools". In an open
 * conversation the session selection is only known once the conversation's
 * saved overrides, or its agent's configuration, have been read: until then
 * the session state is its empty initial value, and sending it would strip
 * the agent of its tools for the turn and for the conversation. A send made
 * before the selection is resolved therefore carries no selection at all.
 */

export interface SessionToolSelection {
  builtin_tool_ids: string[];
  custom_tool_ids: string[];
  integration_ids: string[];
  mcp_server_ids: string[];
  knowledge_base_ids: string[];
}

/** Whether the session selection belongs to the open conversation (always, with none open). */
export function sessionToolsResolved(
  activeConvId: string | null | undefined,
  resolvedForConvId: string | null,
): boolean {
  return !activeConvId || resolvedForConvId === activeConvId;
}

/** The selection to send: the session's once resolved, nothing (the server's defaults) before. */
export function sessionToolSelectionForSend(
  activeConvId: string | null | undefined,
  resolvedForConvId: string | null,
  selection: SessionToolSelection,
): Partial<SessionToolSelection> {
  return sessionToolsResolved(activeConvId, resolvedForConvId) ? selection : {};
}
