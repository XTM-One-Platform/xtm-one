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
 * Workspaces in the chat sidebar.
 *
 * The sidebar lists conversations the way a desktop assistant lists its
 * projects: one collapsible group per workspace, the conversations filed in
 * none below them. From the sidebar a person creates a workspace, starts a
 * conversation inside one, renames one, manages one from the same actions
 * menu as the Workspaces page (`WorkspaceActionsMenu`: open, rename,
 * default, knowledge bases, sharing, archive, delete), and moves a conversation between them -
 * from the row's menu, or by dragging it onto a group.
 *
 * Which groups exist is the panel's own rule (`@filigran/chatbot/workspaces`);
 * every write goes to the endpoints the Workspaces page and the chat already
 * use, so the server's rules (the default is not deleted, an archived
 * workspace takes nothing, a workspace holding work items is not deleted)
 * answer here with their own words. Workspaces are an Enterprise Edition feature: without it the sidebar
 * is the flat list it always was.
 */
import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import {
  ChevronRight,
  Folder,
  FolderInput,
  FolderOpen,
  FolderPlus,
  MoreHorizontal,
  Plus,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { CONVERSATION_DRAG_TYPE, type SidebarWorkspaces } from "@/pages/chat/useSidebarWorkspaces";
import { WorkspaceActionsMenu } from "@/components/workspaces/WorkspaceActionsMenu";
import type { Workspace } from "@/types";

/** Drop-target props for an element conversations can be dragged onto. */
function useDropTarget(onDrop: (conversationId: string) => void, accepts: boolean) {
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  const carries = (e: DragEvent) => e.dataTransfer.types.includes(CONVERSATION_DRAG_TYPE);
  return {
    over,
    props: accepts
      ? {
          onDragEnter: (e: DragEvent) => {
            if (!carries(e)) return;
            depth.current += 1;
            setOver(true);
          },
          onDragOver: (e: DragEvent) => {
            if (!carries(e)) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
          },
          onDragLeave: (e: DragEvent) => {
            if (!carries(e)) return;
            depth.current = Math.max(0, depth.current - 1);
            if (depth.current === 0) setOver(false);
          },
          onDrop: (e: DragEvent) => {
            if (!carries(e)) return;
            e.preventDefault();
            depth.current = 0;
            setOver(false);
            const id = e.dataTransfer.getData(CONVERSATION_DRAG_TYPE);
            if (id) onDrop(id);
          },
        }
      : {},
  };
}

/** The section heading of the workspace groups, with its "new workspace" field. */
export function WorkspacesHeading({ sidebar }: { sidebar: SidebarWorkspaces }) {
  const { t_i18n } = useFormatter();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Enter, Escape and the blur that follows the field's unmount all end the
  // same edit: the first one wins, so a create is never sent twice and an
  // Escape is never turned into a create by the blur behind it.
  const doneRef = useRef(false);

  useEffect(() => {
    if (creating) inputRef.current?.focus();
  }, [creating]);

  const start = () => {
    doneRef.current = false;
    setCreating(true);
  };

  const finish = () => {
    doneRef.current = true;
    setCreating(false);
    setName("");
  };

  const commit = async () => {
    if (doneRef.current) return;
    const trimmed = name.trim();
    finish();
    if (!trimmed) return;
    const created = await sidebar.createWorkspace(trimmed);
    // A workspace the person creates here opens: it is where they are about
    // to drop things. A seeded one stays collapsed until opened.
    if (created) sidebar.setGroupOpen(created.id, true);
  };

  return (
    <div className="px-1">
      <div className="flex items-center justify-between px-2 pb-1 pt-2">
        <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">
          {t_i18n("Workspaces")}
        </p>
        <Tip content={t_i18n("New workspace")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            aria-label={t_i18n("New workspace")}
            onClick={start}
          >
            <FolderPlus className="h-3.5 w-3.5 text-default-secondary" />
          </Button>
        </Tip>
      </div>
      {creating && (
        <form
          className="px-2 pb-1"
          onSubmit={(e) => {
            e.preventDefault();
            void commit();
          }}
        >
          <Input
            ref={inputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => void commit()}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                finish();
              }
            }}
            maxLength={200}
            placeholder={t_i18n("Workspace name")}
            aria-label={t_i18n("Workspace name")}
            className="h-7 content-compact"
          />
        </form>
      )}
    </div>
  );
}

/**
 * One workspace group: its header (collapse, name, count, actions) and, when
 * open, its conversations. The whole group is a drop target.
 */
export function WorkspaceGroupSection({
  sidebar,
  workspace,
  groupKey,
  count,
  open,
  onNewConversation,
  canCreateConversation,
  children,
}: {
  sidebar: SidebarWorkspaces;
  /** `null` for the group of conversations filed where the list does not reach. */
  workspace: Workspace | null;
  groupKey: string;
  /** `undefined` while the conversations load: no count is drawn. */
  count?: number;
  open: boolean;
  onNewConversation: (workspaceId: string) => void;
  canCreateConversation: boolean;
  children: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // One rename per edit: see `WorkspacesHeading`.
  const doneRef = useRef(false);
  const manageable = !!workspace?.can_manage;
  const drop = useDropTarget(
    (conversationId) => {
      if (workspace) void sidebar.fileConversation(conversationId, workspace.id);
    },
    manageable,
  );

  useEffect(() => {
    if (renaming) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [renaming]);

  const commitRename = () => {
    if (doneRef.current) return;
    doneRef.current = true;
    const next = draft.trim();
    setRenaming(false);
    if (workspace && next && next !== workspace.name) void sidebar.renameWorkspace(workspace.id, next);
  };

  const cancelRename = () => {
    doneRef.current = true;
    setRenaming(false);
  };

  const isMyDefault = !!workspace && workspace.is_default && workspace.user_id === user?.id;
  // The shared menu's actions, in the Workspaces card's order; the
  // sidebar only renames in place.
  const startRename = () => {
    if (!workspace) return;
    doneRef.current = false;
    setDraft(workspace.name);
    setRenaming(true);
  };

  const Icon = open ? FolderOpen : Folder;
  return (
    <div
      {...drop.props}
      className={cn(
        "rounded-sm transition-colors",
        drop.over && "bg-accent/10 ring-1 ring-accent/40",
      )}
      data-workspace-group={groupKey}
    >
      <div className="group/ws flex items-center gap-1 rounded-sm px-1.5 py-1 hover:bg-elevation-hover">
        {renaming ? (
          // The rename field takes the toggle's place: an input inside a
          // <button> is invalid, and Firefox drops its clicks and focus.
          <form
            className="flex min-w-0 flex-1 items-center gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              commitRename();
            }}
          >
            <ChevronRight
              className={cn("h-3 w-3 shrink-0 text-default-secondary transition-transform", open && "rotate-90")}
            />
            <Icon className="h-3.5 w-3.5 shrink-0 text-accent/80" />
            <Input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.stopPropagation();
                  cancelRename();
                }
              }}
              maxLength={200}
              aria-label={t_i18n("Workspace name")}
              className="h-6 min-w-0 flex-1 px-1.5 content-compact"
            />
          </form>
        ) : (
          <button
            type="button"
            onClick={() => sidebar.toggle(groupKey)}
            aria-expanded={open}
            className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          >
            <ChevronRight
              className={cn("h-3 w-3 shrink-0 text-default-secondary transition-transform", open && "rotate-90")}
            />
            <Icon className="h-3.5 w-3.5 shrink-0 text-accent/80" />
            <span className="truncate content-compact font-medium text-default-secondary">
              {workspace ? workspace.name : t_i18n("Other workspaces")}
            </span>
            {isMyDefault && (
              <span className="shrink-0 rounded-sm bg-accent/10 px-1 text-[9px] text-accent">{t_i18n("default")}</span>
            )}
            {count !== undefined && (
              <span className="ml-auto shrink-0 pr-1 content-caption tabular-nums text-default-secondary">{count}</span>
            )}
          </button>
        )}
        {workspace && !renaming && (
          <div className="flex shrink-0 items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover/ws:opacity-100">
            {manageable && canCreateConversation && (
              <Tip content={t_i18n("New conversation in this workspace")}>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  aria-label={t_i18n("New conversation in this workspace")}
                  onClick={() => onNewConversation(workspace.id)}
                >
                  <Plus className="h-3.5 w-3.5 text-default-secondary" />
                </Button>
              </Tip>
            )}
            <WorkspaceActionsMenu
              workspace={workspace}
              onRename={startRename}
              side="auto"
              className="h-6 w-6"
              trigger={<MoreHorizontal className="h-3.5 w-3.5 text-default-secondary" />}
            />
          </div>
        )}
      </div>
      {open && <div className="space-y-0.5 pb-1 pl-3">{children}</div>}
    </div>
  );
}

/**
 * The section of conversations filed in no workspace; dropping here unfiles,
 * and its "+" starts a conversation filed in none.
 */
export function UnfiledSection({
  sidebar,
  count,
  onNewConversation,
  canCreateConversation,
  children,
}: {
  sidebar: SidebarWorkspaces;
  /** `undefined` while the conversations load: no count is drawn. */
  count?: number;
  onNewConversation: () => void;
  canCreateConversation: boolean;
  children: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const drop = useDropTarget((conversationId) => void sidebar.fileConversation(conversationId, null), true);
  return (
    <div
      {...drop.props}
      className={cn("rounded-sm px-1 transition-colors", drop.over && "bg-accent/10 ring-1 ring-accent/40")}
      data-workspace-group="unfiled"
    >
      <div className="flex items-center justify-between px-2 pb-1 pt-3">
        <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">
          {t_i18n("Not in a workspace")}
        </p>
        <div className="flex items-center gap-1">
          {count !== undefined && (
            <span className="content-caption tabular-nums text-default-secondary">{count}</span>
          )}
          {canCreateConversation && (
            <Tip content={t_i18n("New conversation outside a workspace")}>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                aria-label={t_i18n("New conversation outside a workspace")}
                onClick={() => onNewConversation()}
              >
                <Plus className="h-3.5 w-3.5 text-default-secondary" />
              </Button>
            </Tip>
          )}
        </div>
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

/** The row's "Move to workspace" menu. */
export function MoveToWorkspaceMenu({
  sidebar,
  conversationId,
  currentWorkspaceId,
}: {
  sidebar: SidebarWorkspaces;
  conversationId: string;
  currentWorkspaceId: string | null;
}) {
  const { t_i18n } = useFormatter();
  const items: OverflowMenuItem[] = [
    ...sidebar.fileable.map((w) => ({
      id: `ws-${w.id}`,
      label: w.name,
      icon: Folder,
      disabled: w.id === currentWorkspaceId,
      onSelect: () => void sidebar.fileConversation(conversationId, w.id),
    })),
    {
      id: "unfile",
      label: t_i18n("Remove from workspace"),
      icon: FolderInput,
      disabled: !currentWorkspaceId,
      separatorBefore: sidebar.fileable.length > 0,
      onSelect: () => void sidebar.fileConversation(conversationId, null),
    },
  ];
  return (
    <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <OverflowMenu
        items={items}
        label={t_i18n("Move to workspace")}
        className="h-6 w-6"
        side="auto"
        scroll
        header={t_i18n("Move to workspace")}
        trigger={
          <Tip content={t_i18n("Move to workspace")}>
            <FolderInput className="h-3 w-3 text-default-secondary hover:text-default-primary" />
          </Tip>
        }
      />
    </span>
  );
}
