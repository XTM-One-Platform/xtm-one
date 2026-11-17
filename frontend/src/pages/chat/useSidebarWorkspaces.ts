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
 * The chat sidebar's workspace state and actions, used by
 * `ConversationSidebar` and the group components in `WorkspaceGroups.tsx`.
 * Kept apart from the components so the component module exports components
 * only (fast refresh).
 */
import { useCallback, useMemo, useRef, useState, type DragEvent } from "react";
import { fileableWorkspaces, parseWorkspaces, type ChatWorkspace } from "@filigran/chatbot/workspaces";

import { useConfirmSharedFiling } from "@/components/chat/useConfirmSharedFiling";
import { useToast } from "@/components/ui/toast";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useFileConversation } from "@/hooks/api/useChatApi";
import {
  useCreateWorkspace,
  useUpdateWorkspace,
  useWorkspaces,
} from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { Workspace } from "@/types";

/**
 * A workspace as the conversation list groups it (`@filigran/chatbot/workspaces`,
 * the panel's own rule), its other fields kept for the group's menus.
 */
type SidebarWorkspace = Workspace & ChatWorkspace;

/** What a dragged conversation row carries. */
export const CONVERSATION_DRAG_TYPE = "application/x-xtm-conversation";

/**
 * Where the expanded groups are remembered in the browser: the fallback of the
 * person's own preference (`User.chat_sidebar_expanded_workspaces`, #9179),
 * read while `/auth/me` has not answered or when it never stored one.
 */
export const EXPANDED_STORAGE_KEY = "xtm-chat-sidebar-expanded-workspaces";

function stringKeys(value: unknown): string[] | null {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : null;
}

function readStoredExpanded(): string[] | null {
  try {
    const raw = window.localStorage.getItem(EXPANDED_STORAGE_KEY);
    return raw ? stringKeys(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}

function writeStoredExpanded(keys: Set<string>) {
  try {
    window.localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify([...keys]));
  } catch {
    // A private window or blocked storage: the server copy still holds it.
  }
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The sidebar's workspace state and actions, in one place. */
export function useSidebarWorkspaces() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const licensed = useAgenticAIEnabled();
  const { data, isSuccess } = useWorkspaces({ enabled: licensed, throwOnError: false });
  const create = useCreateWorkspace();
  const update = useUpdateWorkspace();
  const file = useFileConversation();
  const [filingConfirm, confirmShared] = useConfirmSharedFiling();
  const savePreference = useUpdateProfile();
  // The groups opened during this visit, ahead of the server's answer.
  const [opened, setOpened] = useState<Set<string> | null>(null);
  const [stored] = useState(readStoredExpanded);
  const saved = user?.chat_sidebar_expanded_workspaces;

  // Every group starts collapsed: the person's own choice wins (this visit,
  // then their profile, then this browser), and a group they never opened,
  // a newly seeded workspace included, stays closed.
  const expanded: ReadonlySet<string> = useMemo(
    () => opened ?? new Set(stringKeys(saved) ?? stored ?? []),
    [opened, saved, stored],
  );

  // The list read as the panel reads its own: archived ones left out, the
  // caller's own told by its owner.
  const workspaces: SidebarWorkspace[] = useMemo(() => {
    const byId = new Map((data ?? []).map((w) => [w.id, w] as const));
    return parseWorkspaces(data ?? [], { userId: user?.id }).flatMap((w) => {
      const full = byId.get(w.id);
      return full ? [{ ...full, ...w }] : [];
    });
  }, [data, user?.id]);
  const fileable = useMemo(() => fileableWorkspaces(workspaces), [workspaces]);

  // The latest set, for a call made after an await (the folder+ create).
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;

  const { mutate: persistExpanded } = savePreference;
  const setGroupOpen = useCallback(
    (key: string, open: boolean) => {
      const current = expandedRef.current;
      if (current.has(key) === open) return;
      const next = new Set(current);
      if (open) next.add(key);
      else next.delete(key);
      expandedRef.current = next;
      setOpened(next);
      writeStoredExpanded(next);
      persistExpanded({ chat_sidebar_expanded_workspaces: [...next] });
    },
    [persistExpanded],
  );

  const toggle = useCallback(
    (key: string) => setGroupOpen(key, !expandedRef.current.has(key)),
    [setGroupOpen],
  );

  const fail = useCallback(
    (title: string, err: unknown) =>
      toast({ title, description: errorText(err, t_i18n("Unknown error")), variant: "error" }),
    [toast, t_i18n],
  );

  const createWorkspace = useCallback(
    async (name: string): Promise<Workspace | null> => {
      try {
        return await create.mutateAsync({ name });
      } catch (err) {
        fail(t_i18n("The workspace could not be created"), err);
        return null;
      }
    },
    [create, fail, t_i18n],
  );

  const renameWorkspace = useCallback(
    async (id: string, name: string) => {
      try {
        await update.mutateAsync({ id, name });
      } catch (err) {
        fail(t_i18n("The workspace could not be renamed"), err);
      }
    },
    [update, fail, t_i18n],
  );

  const fileConversation = useCallback(
    async (conversationId: string, workspaceId: string | null) => {
      const target = workspaceId ? workspaces.find((w) => w.id === workspaceId) : undefined;
      // A workspace the list does not hold is refused by the endpoint anyway.
      if (target && !(await confirmShared(target, user?.id))) return;
      try {
        await file.mutateAsync({ id: conversationId, workspaceId });
      } catch (err) {
        fail(t_i18n("The conversation could not be filed"), err);
      }
    },
    [file, fail, t_i18n, workspaces, confirmShared, user?.id],
  );

  return {
    /** Workspaces shown at all: licensed and answered. */
    enabled: licensed && isSuccess,
    workspaces,
    fileable,
    /** The group keys shown expanded; every other group is collapsed. */
    expanded,
    toggle,
    /** Open or close one group (a no-op when it already is). */
    setGroupOpen,
    createWorkspace,
    renameWorkspace,
    fileConversation,
    /** The FR-022 question `fileConversation` asks; render it once. */
    filingConfirm,
  };
}

export type SidebarWorkspaces = ReturnType<typeof useSidebarWorkspaces>;

/** The drag handle props of a conversation row. */
export function conversationDragProps(conversationId: string, enabled: boolean) {
  if (!enabled) return {};
  return {
    draggable: true,
    onDragStart: (e: DragEvent) => {
      e.dataTransfer.setData(CONVERSATION_DRAG_TYPE, conversationId);
      e.dataTransfer.effectAllowed = "move";
    },
  };
}
