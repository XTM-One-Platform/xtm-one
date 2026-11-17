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
 * Filing a conversation into a workspace.
 *
 * The gap an end-to-end run exposed: a workspace could only ever hold
 * threads STARTED inside it, because filing existed at creation and nowhere
 * else. So the report an investigation writes into its workspace was
 * unreachable for every conversation that already existed.
 *
 * Unfiling is offered as plainly as filing. A workspace is a scope somebody
 * chose, and a thread that no longer belongs in one has to be able to leave —
 * `null` is a first-class answer here, not an error state.
 *
 * Archived workspaces are not listed: archiving hides a workspace from every
 * picker, and the endpoint refuses one anyway, so offering it would be a
 * choice that can only fail. Neither are workspaces the caller may only read:
 * filing takes the manage right (`can_manage`).
 *
 * Filing into a workspace other people can see asks first (FR-022):
 * its members can then search the conversation.
 */
import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { useConfirmSharedFiling } from "@/components/chat/useConfirmSharedFiling";
import { useFileConversation } from "@/hooks/api/useChatApi";
import { useWorkspaces } from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Workspace } from "@/types";

export function FileConversationDialog({
  conversationId,
  currentWorkspaceId,
  open,
  onClose,
}: {
  conversationId: string | null;
  /** Where the thread sits now, so the dialog opens on the current answer. */
  currentWorkspaceId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // Fetched while the dialog is open only: it is mounted on every chat page.
  // It shares the sidebar's query, so a failed read reaches this observer even
  // while closed: `throwOnError: false` keeps that failure in the dialog's own
  // line below instead of handing the Chat page to the error boundary.
  const { data: workspaces, isLoading, isError } = useWorkspaces({ enabled: open, throwOnError: false });
  const file = useFileConversation();
  const { user } = useAuth();
  const [ConfirmEl, askShared] = useConfirmSharedFiling();
  const [pending, setPending] = useState<string | null>(null);

  const choose = async (workspace: Workspace | null) => {
    const workspaceId = workspace?.id ?? null;
    if (!conversationId || workspaceId === currentWorkspaceId) {
      onClose();
      return;
    }
    if (workspace && !(await askShared(workspace, user?.id))) return;
    setPending(workspaceId ?? "__none__");
    file.mutate(
      { id: conversationId, workspaceId },
      {
        onSuccess: () => {
          setPending(null);
          onClose();
        },
        onError: (err) => {
          setPending(null);
          // The endpoint refuses for reasons the user can act on — an
          // archived workspace, one that is not theirs to use — so its own
          // message is shown rather than a generic failure.
          toast({
            title: t_i18n("The conversation could not be filed"),
            description: err.message,
            variant: "error",
          });
        },
      },
    );
  };

  const rows = (workspaces ?? []).filter((w) => !w.is_archived && w.can_manage);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("File in a workspace")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Agents working in the same workspace share what they collect there, including the reports an investigation files.",
            )}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center gap-2 py-6 content-compact text-default-secondary">
            <Spinner size="sm" />
            {t_i18n("Loading workspaces…")}
          </div>
        ) : isError ? (
          <p className="py-6 content-compact text-feedback-error-primary">
            {t_i18n("The workspaces could not be loaded.")}
          </p>
        ) : (
          <ul className="max-h-72 space-y-1 overflow-y-auto">
            {/* Unfiling first: it is the answer for a thread that belongs
                nowhere in particular, which is most of them. */}
            <li>
              <WorkspaceRow
                label={t_i18n("No workspace")}
                hint={t_i18n("Nothing is shared through a workspace.")}
                selected={currentWorkspaceId === null}
                busy={pending === "__none__"}
                onSelect={() => choose(null)}
              />
            </li>
            {rows.map((w) => (
              <li key={w.id}>
                <WorkspaceRow
                  label={w.name}
                  hint={w.description || undefined}
                  selected={currentWorkspaceId === w.id}
                  busy={pending === w.id}
                  onSelect={() => choose(w)}
                />
              </li>
            ))}
          </ul>
        )}

        <div className="flex justify-end pt-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
        </div>
        {ConfirmEl}
      </DialogContent>
    </Dialog>
  );
}

function WorkspaceRow({
  label,
  hint,
  selected,
  busy,
  onSelect,
}: {
  label: string;
  hint?: string;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
}) {
  return (
    // data-ds-exempt: a selectable row of a radio list (a card with a label
    // and a hint), not a button; the design system has no radio-card.
    <button
      type="button"
      data-ds-exempt="radio-card"
      role="radio"
      aria-checked={selected}
      disabled={busy}
      onClick={onSelect}
      className={cn(
        "flex w-full items-start gap-2 rounded-sm border px-3 py-2 text-left transition-colors",
        selected ? "border-accent/50 bg-accent/5" : "border-elevation-subtle hover:bg-elevation-highlight",
      )}
    >
      <span className="flex size-4 shrink-0 items-center justify-center">
        {busy && <Spinner size="sm" />}
        {!busy && selected && <Icon name="check" size={16} className="text-accent" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate content-compact font-medium text-default-primary">
          {label}
        </span>
        {hint && (
          <span className="mt-0.5 block truncate content-caption text-default-secondary">
            {hint}
          </span>
        )}
      </span>
    </button>
  );
}
