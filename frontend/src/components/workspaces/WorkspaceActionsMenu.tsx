// SPDX-License-Identifier: Apache-2.0

/**
 * The actions menu of a workspace: `Open workspace`, `Rename`,
 * `Make default`, `Knowledge bases`, `Share`, `Archive` / `Restore` and
 * `Delete`, with the dialogs they open. A card click opens the workspace;
 * every action lives here, in the same order on every surface.
 *
 * One component for every surface that lists workspaces - a card of the
 * Workspaces tab, a group of the chat sidebar - so a workspace offers the same
 * actions, under the same rules, wherever it is managed from. A surface adds
 * its own entries around them (`leadingItems`, `trailingItems`); `Delete`
 * always comes last.
 *
 * Every action the caller's right does not cover is disabled rather than
 * offered and then refused: `can_manage` (`canManageEntity`), and `Make
 * default` on a workspace they do not own. The default workspace cannot be
 * archived or deleted (the API refuses both), and a workspace holding work
 * items cannot be deleted (RESTRICT, a 409) - known only where the list was
 * read with its contents, so the delete confirmation says it for the others.
 *
 * Archiving asks first (restoring does not): an archived workspace leaves the
 * chat sidebar and every picker, and nothing in the sidebar brings it back.
 *
 * The share dialog is given the workspace as it was when the menu opened it,
 * not the live list row: it seeds its draft once, at mount, and compares the
 * draft with its `workspace` prop on every render - a refetch while it is
 * open (window focus, a colleague's change) would move that baseline, and
 * `Save` would write the stale draft over the change.
 */
import { workspacePath } from "@/lib/task-routes";
import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Archive,
  ArchiveRestore,
  BookOpen,
  FolderKanban,
  Pencil,
  Share2,
  Star,
  Trash2,
} from "lucide-react";

import { useConfirm } from "@/components/ui/confirm-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import {
  useDeleteWorkspace,
  useSetDefaultWorkspace,
  useUpdateWorkspace,
} from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { canManageEntity } from "@/lib/utils";
import { WorkspaceDialog } from "@/components/workspaces/WorkspaceDialog";
import { WorkspaceKnowledgeBaseDialog } from "@/components/workspaces/WorkspaceKnowledgeBaseDialog";
import { WorkspaceShareDialog } from "@/components/workspaces/WorkspaceShareDialog";
import { hostedFleetCount, liveHostedFleetCount } from "@/lib/workspace-hosted-fleets";
import type { Workspace } from "@/types";

export function WorkspaceActionsMenu({
  workspace,
  leadingItems = [],
  trailingItems = [],
  side,
  className,
  trigger,
  openWorkspace = true,
  onRename,
}: {
  workspace: Workspace;
  /** `Open workspace`; off on the workspace page itself. */
  openWorkspace?: boolean;
  /** `Rename` in place (the chat sidebar); otherwise it opens the rename dialog. */
  onRename?: () => void;
  /** The surface's own entries, before the shared ones. */
  leadingItems?: OverflowMenuItem[];
  /** The surface's own entries, after the shared ones and before `Delete`. */
  trailingItems?: OverflowMenuItem[];
  side?: "bottom" | "top" | "auto";
  className?: string;
  trigger?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { user, isAdmin } = useAuth();
  const [confirmNode, confirm] = useConfirm();
  const [binding, setBinding] = useState(false);
  const [sharing, setSharing] = useState<Workspace | null>(null);
  const [renaming, setRenaming] = useState<Workspace | null>(null);
  const update = useUpdateWorkspace();
  const remove = useDeleteWorkspace();
  const setDefault = useSetDefaultWorkspace();

  const w = workspace;
  const manage = canManageEntity(w, user?.id, isAdmin);
  // A fleet lives in its workspace: the API refuses to delete a
  // workspace hosting any fleet and to archive one hosting a live fleet, so
  // the menu says it before the click, in the API's words.
  // One fleet is named; one the reader cannot see, or several (a
  // fallback the model no longer creates), are counted.
  const hosted = hostedFleetCount(w);
  const liveHosted = liveHostedFleetCount(w);
  const liveFleetName = (w.hosted_fleets ?? []).find((f) => !f.is_archived)?.name;
  const fleetName = (w.hosted_fleets ?? [])[0]?.name;
  const archiveRefusal =
    !w.is_archived && liveHosted > 0
      ? liveHosted === 1
        ? liveFleetName
          ? t_i18n("This workspace hosts the fleet {name}: archive the fleet or move it to another workspace first.", { name: liveFleetName })
          : t_i18n("This workspace hosts a fleet you cannot see: its owner archives it or moves it to another workspace first.")
        : t_i18n("This workspace hosts {n} fleets: archive or move them first.", { n: liveHosted })
      : undefined;
  const deleteRefusal =
    hosted > 0
      ? hosted === 1
        ? fleetName
          ? t_i18n("This workspace hosts the fleet {name}: move the fleet to another workspace or delete it first.", { name: fleetName })
          : t_i18n("This workspace hosts a fleet you cannot see: its owner moves it to another workspace or deletes it first.")
        : t_i18n("This workspace hosts {n} fleets: move them to another workspace or delete them first.", { n: hosted })
      : undefined;
  // Work items FILED here block the delete too (RESTRICT): said before the
  // click, like the fleets. Not the fleet's population: an item a
  // member owns elsewhere never blocks this workspace.
  const heldWorkItems = w.contents?.held_work_items ?? 0;
  const workItemsRefusal =
    heldWorkItems > 0
      ? heldWorkItems === 1
        ? t_i18n("This workspace still holds 1 work item: archive it instead, or clear its work items first.")
        : t_i18n("This workspace still holds {n} work items: archive it instead, or clear its work items first.", { n: heldWorkItems })
      : undefined;

  const run = async (action: () => Promise<unknown>, fallback: string) => {
    try {
      await action();
    } catch (err) {
      toast({
        title: t_i18n("Error"),
        description: err instanceof Error && err.message ? err.message : fallback,
        variant: "error",
      });
    }
  };

  const items: OverflowMenuItem[] = [
    ...(openWorkspace
      ? [
          {
            id: "open-workspace",
            label: t_i18n("Open workspace"),
            icon: FolderKanban,
            onSelect: () => navigate(workspacePath(workspace.id)),
          },
        ]
      : []),
    {
      id: "rename",
      label: t_i18n("Rename"),
      icon: Pencil,
      disabled: !manage,
      onSelect: () => (onRename ? onRename() : setRenaming(w)),
    },
    ...leadingItems,
    {
      id: "default",
      label: t_i18n("Make default"),
      icon: Star,
      // Refused by the API for an archived workspace, a company-managed one
      // and one the caller does not own, and pointless for the one that
      // already is.
      disabled:
        w.is_default || w.is_archived || w.is_company_managed || w.user_id !== user?.id,
      onSelect: () =>
        run(
          () => setDefault.mutateAsync(w.id),
          t_i18n("Could not change the default workspace."),
        ),
    },
    {
      id: "knowledge",
      label: t_i18n("Knowledge bases"),
      icon: BookOpen,
      disabled: !manage,
      onSelect: () => setBinding(true),
    },
    {
      id: "share",
      label: t_i18n("Share"),
      icon: Share2,
      disabled: !manage,
      onSelect: () => setSharing(w),
    },
    {
      id: "archive",
      label: w.is_archived ? t_i18n("Restore") : t_i18n("Archive"),
      icon: w.is_archived ? ArchiveRestore : Archive,
      separatorBefore: true,
      // The default must stay reachable: it is where a user lands. A live
      // fleet's coordination must stay open to new work.
      disabled: w.is_default || !manage || !!archiveRefusal,
      disabledReason: manage && !w.is_default ? archiveRefusal : undefined,
      onSelect: async () => {
        if (!w.is_archived) {
          const ok = await confirm({
            title: t_i18n("Archive this workspace?"),
            description: t_i18n(
              "It leaves the chat sidebar and every picker, and its conversations are listed under Other workspaces. You can restore it from Work > Operations > Workspaces.",
            ),
            confirmLabel: t_i18n("Archive"),
            variant: "warning",
          });
          if (!ok) return;
        }
        await run(
          () => update.mutateAsync({ id: w.id, is_archived: !w.is_archived }),
          t_i18n("Could not archive this workspace."),
        );
      },
    },
    ...trailingItems,
    {
      id: "delete",
      label: t_i18n("Delete"),
      icon: Trash2,
      destructive: true,
      separatorBefore: trailingItems.length > 0,
      // Work items and hosted fleets block the delete (RESTRICT): the API
      // answers 409.
      disabled: w.is_default || !manage || !!workItemsRefusal || !!deleteRefusal,
      disabledReason: manage && !w.is_default ? deleteRefusal ?? workItemsRefusal : undefined,
      onSelect: async () => {
        const ok = await confirm({
          title: t_i18n("Delete this workspace?"),
          description: t_i18n(
            "Its conversations stay, in no workspace, and bound knowledge bases are left untouched. A workspace that still holds work items cannot be deleted: archive it instead.",
          ),
          confirmLabel: t_i18n("Delete"),
          variant: "danger",
        });
        if (!ok) return;
        await run(
          () => remove.mutateAsync(w.id),
          t_i18n("Could not delete this workspace."),
        );
      },
    },
  ];

  return (
    <>
      {confirmNode}
      <OverflowMenu
        items={items}
        label={t_i18n("Workspace actions")}
        side={side}
        className={className}
        trigger={trigger}
      />
      {binding && (
        <WorkspaceKnowledgeBaseDialog workspace={w} onClose={() => setBinding(false)} />
      )}
      {/* Mounted per opening, with the workspace as it was then: a cancelled
          edit never leaks into the next one, and a refetch never moves the
          baseline the draft is compared with. */}
      {sharing && (
        <WorkspaceShareDialog workspace={sharing} onClose={() => setSharing(null)} />
      )}
      {renaming && (
        <WorkspaceDialog key={renaming.id} workspace={renaming} open onClose={() => setRenaming(null)} />
      )}
    </>
  );
}
