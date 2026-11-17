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
 * What the /chat page's composer says and takes: its placeholder, what Deep
 * Investigation does with a send, and the workspace a move files a
 * conversation into. Pure, so `composer-rules.test.ts` runs under `node --test`.
 */
import type { ChatSendDraft } from "@filigran/chatbot";

type Translate = (key: string, values?: Record<string, string>) => string;

/**
 * The composer's placeholder: a subject while Deep Investigation is on, else
 * the agent a message goes to (`agentName`), else (the General Assistant,
 * `null`) the platform, with the hint that `@` mentions another agent or a
 * conversation.
 */
export function composerPlaceholder(
  agentName: string | null,
  { deepInvestigation, platformName }: { deepInvestigation: boolean; platformName: string },
  t: Translate,
): string {
  if (deepInvestigation) return t("Enter a subject to investigate…");
  if (agentName) return t("Message {name}…", { name: agentName });
  return t("Message {name}… (type @ to mention an agent or a conversation)", { name: platformName || t("Assistant") });
}

/**
 * The agent the placeholder names: an open conversation's own, which answers
 * it whichever agent the panel's menu picked (one continued with another
 * agent, one reopened from the list), `null` for the General Assistant's;
 * else, with no conversation open (`undefined`), the agent picked
 * (`pickedName`). A conversation's agent missing from *agents* keeps the
 * picked one's name.
 */
export function placeholderAgentName(
  conversationAgentId: string | null | undefined,
  agents: readonly { id: string; name: string }[] | undefined,
  pickedName: string | null,
): string | null {
  if (conversationAgentId === undefined) return pickedName;
  if (conversationAgentId === null) return null;
  return agents?.find((a) => a.id === conversationAgentId)?.name ?? pickedName;
}

/**
 * What Deep Investigation does with a send while it is on: a message typed in
 * the composer, no turn running, is its subject (`"subject"`, taken over to
 * the source-pack picker), unless it carries files (`"files"`, refused: the
 * investigation queries source packs and never reads files). Any other send
 * goes as it is (`null`).
 */
export function deepInvestigationTakes(
  draft: Pick<ChatSendDraft, "source" | "turnRunning" | "fileCount">,
  on: boolean,
): "subject" | "files" | null {
  if (!on || draft.source !== "composer" || draft.turnRunning) return null;
  return (draft.fileCount ?? 0) > 0 ? "files" : "subject";
}

/**
 * The platform's workspace a move files a conversation into (the panel's
 * `workspace`, `null` taking it out of one), whose sharing the move asks
 * about; none when it is not in the list read.
 */
export function filingTarget<W extends { id: string }>(
  workspace: { id: string } | null,
  workspaces: readonly W[] | undefined,
): W | undefined {
  return workspace ? workspaces?.find((w) => w.id === workspace.id) : undefined;
}
