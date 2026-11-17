// SPDX-License-Identifier: Apache-2.0

/**
 * Share a workspace with people and groups.
 *
 * A workspace is shared like every other entity: per-person and per-group
 * grants with a tier (`PrincipalSharingCard`), plus the admin-only
 * company-managed switch, whose audience a restriction narrows. What
 * a grant means is particular to a workspace, so the tiers say it: `Read only`
 * shows the workspace and what its agents built there; `Can configure` also
 * lets the grantee file their own conversations into it - their agents then
 * write its work items and shared state - change it, share it and delete it
 * (the server's `can_manage`, the rule of every other entity).
 *
 * The default workspace is its owner's landing place: it can be shared, never
 * made company-managed (`ensure_can_be_company_managed` refuses it), so the
 * switch is disabled for it with the reason instead of offered and refused.
 */
import { useState } from "react";
import { Building2, Lock, Share2, UsersRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CompanyManagedSwitch } from "@/components/ui/company-managed-switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { useToast } from "@/components/ui/toast";
import { useUpdateWorkspace } from "@/hooks/api/useWorkspaceApi";
import { useAuth } from "@/hooks/useAuth";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import { WORKSPACE_SHARE_PERMISSION_DESCRIPTIONS } from "@/lib/constants";
import {
  sharingAudience,
  sharingChanged,
  sharingPayloadGroups,
  type SharingDraft,
} from "@/lib/sharing-draft";
import type { Workspace } from "@/types";
import { Spinner } from "@filigran/design-system";

export function WorkspaceShareDialog({
  workspace,
  onClose,
}: {
  /** The workspace to share. Mount the dialog keyed by its id: the draft is
   *  seeded from it once, at mount, so the first paint already compares the
   *  draft with the stored sharing. */
  workspace: Workspace;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const update = useUpdateWorkspace();

  // Seeded from the workspace at mount; the tab mounts a fresh dialog each
  // time it opens, so a cancelled edit never leaks into the next session.
  const [groupIds, setGroupIds] = useState<string[] | null>(workspace.shared_group_ids);
  const [groupPermissions, setGroupPermissions] = useState<Record<string, string> | null>(
    workspace.shared_group_permissions,
  );
  const [companyManaged, setCompanyManaged] = useState(workspace.is_company_managed);
  const userGrants = useUserShareGrants(workspace);

  const draft: SharingDraft = {
    companyManaged,
    accessRestricted: userGrants.props.accessRestricted,
    groupIds,
    groupPermissions,
    userIds: userGrants.props.userIds,
    userPermissions: userGrants.props.userPermissions,
  };
  const dirty = sharingChanged(draft, {
    companyManaged: workspace.is_company_managed,
    accessRestricted: workspace.access_restricted ?? false,
    groupIds: workspace.shared_group_ids,
    groupPermissions: workspace.shared_group_permissions,
    userIds: workspace.shared_user_ids ?? null,
    userPermissions: workspace.shared_user_permissions ?? null,
  });
  const { kind, text: audience } = sharingAudience(draft, t_i18n);
  const AudienceIcon = kind === "company" ? Building2 : kind === "shared" ? UsersRound : Lock;
  // Only a default that is still personal is locked: one an older version let
  // become company-managed can still be switched back.
  const companyLocked = workspace.is_default && !workspace.is_company_managed;

  const save = async () => {
    try {
      await update.mutateAsync({
        id: workspace.id,
        is_company_managed: companyManaged,
        ...sharingPayloadGroups(draft),
        ...userGrants.toPayload(companyManaged),
      });
      toast({ title: t_i18n("Sharing updated"), variant: "success" });
      onClose();
    } catch (err) {
      toast({
        title: t_i18n("Could not update sharing"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Share2 className="h-4 w-4 text-accent" />
            {t_i18n("Share workspace")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "People you share it with see it and what its agents built there. With Can configure, they can also file their own conversations into it, change it, share it and delete it.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <PrincipalSharingCard
            sharedGroupIds={groupIds}
            onChange={setGroupIds}
            sharedGroupPermissions={groupPermissions}
            onPermissionsChange={setGroupPermissions}
            userSharing={userGrants.props}
            entityType="workspace"
            isCompanyManaged={companyManaged}
            disabled={update.isPending}
            permissionDescriptions={WORKSPACE_SHARE_PERMISSION_DESCRIPTIONS}
          />

          {isAdmin && (
            <CompanyManagedSwitch
              label={t_i18n("Company-managed workspace")}
              description={
                companyLocked
                  ? t_i18n(
                      "The default workspace stays personal. Make another workspace the default first.",
                    )
                  : t_i18n("Visible to everyone in the company. Only admins can edit.")
              }
              checked={companyManaged}
              onCheckedChange={setCompanyManaged}
              disabled={update.isPending || companyLocked}
            />
          )}

          <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-hover p-3">
            <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">
              {t_i18n("Who can see this workspace")}
            </p>
            <p className="flex items-start gap-2 content-compact">
              <AudienceIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
              <span className="min-w-0">
                {audience}
                {dirty && (
                  <span className="ml-1.5 text-feedback-warning-primary">{t_i18n("Applies when you save.")}</span>
                )}
              </span>
            </p>
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={update.isPending}>
            {dirty ? t_i18n("Cancel") : t_i18n("Close")}
          </Button>
          <Button onClick={() => void save()} disabled={update.isPending || !dirty} className="gap-1.5" aria-busy={update.isPending || undefined}>
            {update.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
