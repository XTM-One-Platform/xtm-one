// SPDX-License-Identifier: Apache-2.0

/**
 * The Share dialog of a shareable work (a video, a custom dashboard): per-user
 * and per-group grants with a tier, the admin-only company-managed switch, the
 * audience line computed from the DRAFT, and the work's address to copy.
 *
 * One draft logic for every such dialog: re-seeded from the entity each time
 * it opens (a colleague or the assistant may have changed the sharing since,
 * and a cancelled edit must not leak into the next session); only the sharing
 * fields travel to `save`, never the work itself. What differs - the words,
 * the tiers' labels, the mutation, a section of its own (a video's public
 * link) - is passed in.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Spinner } from "@filigran/design-system";
import { Building2, Link2, Lock, Share2, UsersRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import { sharingAudience, sharingChanged, sharingPayloadGroups, type SharingDraft } from "@/lib/sharing-draft";

/** What a Share dialog reads of the work it shares. */
export interface ShareableEntity {
  is_company_managed: boolean;
  access_restricted: boolean;
  shared_group_ids: string[];
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids: string[] | null;
  shared_user_permissions: Record<string, string> | null;
  can_manage: boolean;
}

/** The sharing fields a save sends. */
export type SharingPayload = { is_company_managed: boolean } & ReturnType<typeof sharingPayloadGroups> &
  ReturnType<ReturnType<typeof useUserShareGrants>["toPayload"]>;

/** The API sends `[]` for "no group grants"; the sharing card reads `null` as "not shared". */
function storedGroupIds(entity: ShareableEntity): string[] | null {
  return entity.shared_group_ids.length > 0 ? entity.shared_group_ids : null;
}

export function EntityShareDialog({
  entity,
  open,
  onOpenChange,
  title,
  description,
  entityType,
  permissionLabels,
  permissionDescriptions,
  companyLabel,
  link,
  linkNote,
  save,
  saving,
  testId,
  children,
}: {
  entity: ShareableEntity;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  /** The `PrincipalSharingCard`'s entity type ("video", "dashboard"). */
  entityType: string;
  permissionLabels: Record<string, string>;
  permissionDescriptions: Record<string, string>;
  /** The company-managed switch's label ("Company-managed video"). */
  companyLabel: string;
  /** The work's address: it opens for its grants only. */
  link: string;
  /** What anyone else who opens the address is told, when it is worth saying. */
  linkNote?: string;
  save: (payload: SharingPayload) => Promise<unknown>;
  saving: boolean;
  testId: string;
  /** Sections of the work's own, after the address (a video's public link). */
  children?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [groupIds, setGroupIds] = useState<string[] | null>(storedGroupIds(entity));
  const [groupPermissions, setGroupPermissions] = useState<Record<string, string> | null>(entity.shared_group_permissions);
  const [companyManaged, setCompanyManaged] = useState(entity.is_company_managed);
  const userGrants = useUserShareGrants(entity);

  useEffect(() => {
    if (!open) return;
    setGroupIds(storedGroupIds(entity));
    setGroupPermissions(entity.shared_group_permissions);
    setCompanyManaged(entity.is_company_managed);
    userGrants.reset();
    // `userGrants` is a fresh object every render; `reset` is what we call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, entity]);

  const draft: SharingDraft = {
    companyManaged,
    accessRestricted: userGrants.props.accessRestricted,
    groupIds,
    groupPermissions,
    userIds: userGrants.props.userIds,
    userPermissions: userGrants.props.userPermissions,
  };
  const dirty = sharingChanged(draft, {
    companyManaged: entity.is_company_managed,
    accessRestricted: entity.access_restricted,
    groupIds: storedGroupIds(entity),
    groupPermissions: entity.shared_group_permissions,
    userIds: entity.shared_user_ids,
    userPermissions: entity.shared_user_permissions,
  });
  const { kind: audienceKind, text: audience } = sharingAudience(draft, t_i18n);
  const AudienceIcon = audienceKind === "company" ? Building2 : audienceKind === "shared" ? UsersRound : Lock;

  const onSave = async () => {
    try {
      await save({
        is_company_managed: companyManaged,
        ...sharingPayloadGroups(draft),
        ...userGrants.toPayload(companyManaged),
      });
      toast({ title: t_i18n("Sharing updated"), variant: "success" });
      onOpenChange(false);
    } catch (err) {
      toast({
        title: t_i18n("Could not update sharing"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" data-testid={testId}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Share2 className="h-4 w-4 text-accent" />
            {title}
          </DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <PrincipalSharingCard
            sharedGroupIds={groupIds}
            onChange={setGroupIds}
            sharedGroupPermissions={groupPermissions}
            onPermissionsChange={setGroupPermissions}
            userSharing={userGrants.props}
            entityType={entityType}
            isCompanyManaged={companyManaged}
            disabled={saving}
            permissionLabels={permissionLabels}
            permissionDescriptions={permissionDescriptions}
          />

          {isAdmin && (
            <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover p-3">
              <div className="flex min-w-0 items-center gap-2">
                <Building2 className="h-4 w-4 shrink-0 text-accent" />
                <div className="min-w-0">
                  <p className="content-base font-medium">{companyLabel}</p>
                  <p className="content-compact text-default-secondary">
                    {t_i18n("Visible to everyone in the company. Only admins can edit.")}
                  </p>
                </div>
              </div>
              <Switch aria-label={companyLabel} checked={companyManaged} onCheckedChange={setCompanyManaged} disabled={saving} />
            </div>
          )}

          {/* The address comes after the audience it is good for: it is not a
              grant, and whoever copies it must know who it opens for. */}
          <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-hover p-3">
            <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">
              {t_i18n("Who can open this link")}
            </p>
            <p className="flex items-start gap-2 content-compact">
              <AudienceIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
              <span className="min-w-0">
                {audience}
                {dirty && <span className="ml-1.5 text-feedback-warning-primary">{t_i18n("Applies when you save.")}</span>}
              </span>
            </p>
            <div className="flex items-start gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2">
              <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden />
              <code className="min-w-0 flex-1 break-all font-mono-plex text-[11px] leading-snug text-default-secondary">
                {link}
              </code>
              <CopyIconButton value={link} label={t_i18n("Copy link")} className="-my-1 shrink-0" />
            </div>
            {linkNote && <p className="text-[11px] text-default-secondary">{linkNote}</p>}
          </div>

          {children}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
            {dirty ? t_i18n("Cancel") : t_i18n("Close")}
          </Button>
          <Button onClick={() => void onSave()} disabled={saving || !dirty || !entity.can_manage} aria-busy={saving || undefined} className="gap-1.5">
            {saving && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
