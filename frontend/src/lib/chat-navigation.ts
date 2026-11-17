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
 * Where the chat page lands and how its conversation list follows a send.
 *
 * - The "Chat" entry of the navigation resumes a conversation instead of
 *   opening an empty one: the one the person last opened when it is still in
 *   their list, else their most recent (`pickResumeConversation`), on an
 *   arrival or a navigation that asks for no other (`resumesOnArrival`,
 *   `resumesOnNavigation`). While that decision is pending, and until the
 *   picked conversation is open, the page shows a placeholder, never the
 *   welcome screen (#7730, `resolveResume`).
 * - The "Chat" entry clicked on the bare `/chat` itself (after the open
 *   conversation was archived) resumes too: the router turns a link to the
 *   address already shown into a replace (`navigationArmsResume`).
 * - A message sent in an older conversation moves it to the top of its
 *   sidebar section at once (`bumpConversation`), the way the server orders
 *   the list once the next fetch lands (`updated_at` descending).
 *
 * Pure (no React, no DOM global, no `@/` import) so `chat-navigation.test.ts`
 * runs under `node --test`.
 */

/** The fields of a list row these helpers read and write. */
interface ListedConversation {
  id: string;
  is_archived?: boolean;
  updated_at: string;
  last_message_preview?: string | null;
}

/** The browser storage key of the conversation a person last opened. */
export function lastConversationStorageKey(userId: string): string {
  return `xtm-one:chat:last-conversation:${userId}`;
}

/**
 * The conversation "Chat" resumes: `lastOpenedId` while the list still holds
 * it (not archived, not deleted), else the first one the list shows (the most
 * recent), else none - the page then shows a new conversation.
 */
export function pickResumeConversation<C extends ListedConversation>(
  conversations: readonly C[] | undefined,
  lastOpenedId: string | null,
): string | null {
  const live = (conversations ?? []).filter((c) => !c.is_archived);
  if (lastOpenedId && live.some((c) => c.id === lastOpenedId)) return lastOpenedId;
  return live[0]?.id ?? null;
}

/** The address's query, as the resume reads it. */
interface ChatAddress {
  has(name: string): boolean;
}

/** What the address asks for that is not the bare `/chat`: a conversation, a new one, a launch. */
function asksForAChat(params: ChatAddress): boolean {
  return ["c", "new", "agent", "investigate"].some((name) => params.has(name));
}

/**
 * Whether arriving on the chat page resumes a conversation: on the bare
 * `/chat` only, and not when a launcher handed the page an agent or a draft
 * to start a new one with.
 */
export function resumesOnArrival(
  params: ChatAddress,
  launch: { pendingAgent: string | null; draft: string; autoSend: boolean },
): boolean {
  return !asksForAChat(params) && !launch.pendingAgent && !launch.draft.trim() && !launch.autoSend;
}

/** What the page knows when it settles a pending resume. */
interface ResumeInputs<C extends ListedConversation> {
  /** The conversation list; undefined while it loads. */
  conversations: readonly C[] | undefined;
  /** The list read failed. */
  failed: boolean;
  /** The conversation the person last opened (browser storage). */
  lastOpenedId: string | null;
  /** The conversation the address already opens (`?c=`). */
  activeConvId: string | null;
  /** The person started typing in the new conversation. */
  typing: boolean;
}

/** How a pending resume is settled. */
interface ResumeOutcome {
  /** The decision is taken: the resume is no longer armed. */
  settled: boolean;
  /** The conversation to open (`?c=`), if any. */
  open: string | null;
  /**
   * The transcript placeholder stays up. True while the list loads and when
   * a conversation is picked: its `?c=` reaches the page one commit later,
   * and dropping the placeholder in between painted the welcome screen for a
   * few frames. The page clears it once the
   * conversation is open.
   */
  keepPlaceholder: boolean;
}

/**
 * The settlement of a pending resume: wait for the list, open the picked
 * conversation (placeholder kept until it is open), or show the new
 * conversation when there is nothing to resume, the list failed, or the
 * person already moved on.
 */
export function resolveResume<C extends ListedConversation>(inputs: ResumeInputs<C>): ResumeOutcome {
  if (inputs.failed) return { settled: true, open: null, keepPlaceholder: false };
  if (!inputs.conversations) return { settled: false, open: null, keepPlaceholder: true };
  if (inputs.activeConvId || inputs.typing) return { settled: true, open: null, keepPlaceholder: false };
  const open = pickResumeConversation(inputs.conversations, inputs.lastOpenedId);
  return { settled: true, open, keepPlaceholder: open !== null };
}

/** A router navigation, as `useNavigationType` and `useLocation` read it. */
interface ChatNavigation {
  type: "PUSH" | "REPLACE" | "POP";
  /** The address's query before the navigation (`location.search`). */
  previousSearch: string;
  /** The address's query after it. */
  search: string;
}

/**
 * Whether a navigation to the bare `/chat` asks for the resume: a pushed one
 * (a link, the "Chat" entry from another page), or a link to the very address
 * already shown, which the router replaces instead of pushing ("Chat"
 * clicked on the empty page left by archiving the open conversation). The
 * page's own rewrites change the address (`?new=1`, `?investigate=` dropped)
 * and back / forward are `POP`: neither resumes.
 */
export function navigationArmsResume(nav: ChatNavigation): boolean {
  if (nav.type === "PUSH") return true;
  return nav.type === "REPLACE" && nav.previousSearch === nav.search;
}

/**
 * Whether a later navigation on the chat page resumes a conversation: one
 * `navigationArmsResume` arms, to the bare `/chat`, and not the page's own
 * leaving of its conversation (a new one, a deletion).
 */
export function resumesOnNavigation(params: ChatAddress, navigation: ChatNavigation & { leavingConversation: boolean }): boolean {
  return navigationArmsResume(navigation) && !navigation.leavingConversation && !asksForAChat(params);
}

/** What a send changes on the conversation's row. */
interface ConversationBump {
  /** When the message was sent, ISO 8601. */
  at: string;
  /** The row's new preview; omitted, the preview is kept. */
  preview?: string;
}

/** How long a preview the list keeps (the server cuts its own the same way). */
const PREVIEW_MAX_CHARS = 200;

/**
 * The list with conversation `id` moved first and its row touched by a send.
 * The list itself comes back unchanged (same reference) when it does not hold
 * the conversation or is not a list at all, so a cache that does not show it
 * re-renders nothing. The sidebar groups rows by workspace in list order, so
 * first in the list is first in its section.
 */
export function bumpConversation<C extends ListedConversation>(
  list: C[] | undefined,
  id: string,
  bump: ConversationBump,
): C[] | undefined {
  if (!Array.isArray(list)) return list;
  const index = list.findIndex((c) => c.id === id);
  if (index < 0) return list;
  const row = list[index];
  const preview = bump.preview?.trim();
  const touched: C = {
    ...row,
    updated_at: bump.at,
    ...(preview ? { last_message_preview: preview.slice(0, PREVIEW_MAX_CHARS) } : {}),
  };
  return [touched, ...list.slice(0, index), ...list.slice(index + 1)];
}
