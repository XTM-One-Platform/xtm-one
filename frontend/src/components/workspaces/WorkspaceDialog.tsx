// SPDX-License-Identifier: Apache-2.0

/**
 * Create or rename a workspace, one dialog for every surface.
 *
 * The Workspaces tab opens it to create; the workspace actions menu
 * (`WorkspaceActionsMenu`) opens it to rename, from the Workspaces card and
 * the workspace page alike: a card click opens the workspace, never
 * this dialog.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { useCreateWorkspace, useUpdateWorkspace } from "@/hooks/api/useWorkspaceApi";
import { useFormatter } from "@/i18n";
import type { Workspace } from "@/types";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export function WorkspaceDialog({
  workspace,
  open,
  onClose,
}: {
  /** null = create, otherwise rename/edit that workspace. */
  workspace: Workspace | null;
  open: boolean;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateWorkspace();
  const update = useUpdateWorkspace();
  const [name, setName] = useState(workspace?.name ?? "");
  const [description, setDescription] = useState(workspace?.description ?? "");

  const editing = workspace !== null;
  const busy = create.isPending || update.isPending;

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    try {
      if (editing) {
        await update.mutateAsync({
          id: workspace.id,
          name: trimmed,
          description,
        });
      } else {
        await create.mutateAsync({ name: trimmed, description });
      }
      onClose();
    } catch (err) {
      toast({
        title: t_i18n("Error"),
        description: errorMessage(err, t_i18n("Unknown error")),
        variant: "error",
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {editing ? t_i18n("Rename workspace") : t_i18n("New workspace")}
          </DialogTitle>
          <DialogDescription>
            {editing
              ? /* Said here rather than discovered later: the slug is what a
                   link uses, so a rename that silently re-slugged would break
                   every bookmark pointing at the workspace. */
                t_i18n(
                  "The name changes; the handle does not, so existing links keep working.",
                )
              : t_i18n(
                  "A named environment agents can be pointed at, so they share what they collect.",
                )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="workspace-name">{t_i18n("Name")}</Label>
            <Input aria-label={t_i18n("Name")}
              id="workspace-name"
              value={name}
              autoFocus
              placeholder={t_i18n("Threat research")}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
            />
            {editing && (
              <p className="content-caption text-default-secondary">
                {t_i18n("Handle")}: <code>{workspace.slug}</code>
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="workspace-description">
              {t_i18n("Description")}
            </Label>
            <Input aria-label={t_i18n("Description")}
              id="workspace-description"
              value={description}
              placeholder={t_i18n("What this workspace is for")}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button disabled={!name.trim() || busy} onClick={submit}>
            {editing ? t_i18n("Save") : t_i18n("Create")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
