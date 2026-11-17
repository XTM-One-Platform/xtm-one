// SPDX-License-Identifier: Apache-2.0

/**
 * EntityConfigCard — shared configuration card shell for entity detail pages.
 *
 * Encapsulates the repeated pattern across prompt, skill, custom-tool,
 * mcp-server, integration, and knowledge-base ConfigTabs:
 *   - Card header with Settings2 icon + "Configuration" + Edit / ReadOnlyBadge
 *   - Read view banners (CompanyManagedBanner, SharedBanner)
 *   - Edit wrapper (cyan-tinted container with heading)
 *   - Group sharing (any user who can manage) + company-managed toggle (admin only)
 *   - Save / Cancel buttons
 *
 * Entity-specific fields are passed via `readView` and `editView` render slots.
 */

import { Settings2, Pencil, Globe } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { SharedBanner } from "@/components/ui/shared-banner";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { OwnerCard } from "@/components/ui/owner-card";
import { CompanyManagedSwitch } from "@/components/ui/company-managed-switch";
import { useAuth } from "@/hooks/useAuth";
import type { UserSharingProps } from "@/hooks/useUserShareGrants";
import { configLockReason } from "@/lib/utils";
import { useAICatalogEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

interface EntityConfigCardProps {
  /** Display label for the entity type (e.g. "prompt", "skill", "MCP server"). */
  entityLabel: string;
  /** Whether the entity is company-managed. */
  isCompanyManaged: boolean;
  /** Seeded by the platform and re-synced on boot: read-only for everyone,
   *  admins included, so the banner says "built-in" instead of promising an
   *  admin an edit the API will refuse. Locks the card on its own. */
  isBuiltin?: boolean;
  /** Installed by a skill package (`is_package_origin`): the write
   *  routes refuse every change whoever asks, so the card is locked and says
   *  the package is what to change. */
  isPackageOrigin?: boolean;
  /** The entity's owning user ID (null for company-managed). */
  userId: string | null;
  /** The entity's shared group IDs (used to determine if the "Shared" badge applies). */
  sharedGroupIds?: string[] | null;
  /** Display name of the entity owner (passed through to SharedBanner). */
  ownerName?: string | null;

  // -- Permissions --
  /** Whether the current user can manage (edit) this entity: `canManageEntity()`
   *  (the server's tier-aware `can_manage`) plus any page-specific lock. */
  canManage: boolean;
  /** The viewer's own right to manage it, before any page-specific lock
   *  (`canManageEntity()`). Defaults to `canManage`. On someone else's shared
   *  entity, `false` is explained as a read-only share - pass it when
   *  the page adds a lock of its own (registration-only, system-managed), so
   *  that lock is never presented as a missing share permission, nor an admin
   *  told that only admins may edit. The page explains its lock in `readView`. */
  hasManageRights?: boolean;
  /** Additional gate beyond canManage (e.g. KB's !is_system_managed). Default true. */
  canEdit?: boolean;

  // -- Edit lifecycle --
  editing: boolean;
  onStartEditing: () => void;
  onCancel: () => void;
  onSave: () => void;
  isSaving: boolean;
  /**
   * Whether saving this entity needs AI. Defaults to `true` on purpose: the
   * card is shared by prompts, skills, knowledge bases, MCP servers and
   * Custom tools, and only the last is deterministic, so a caller must
   * *declare* itself exempt rather than inherit it.
   */
  requiresAi?: boolean;
  /** Whether the save button should be enabled. Default true. */
  canSave?: boolean;

  // -- Company-managed + group sharing (edit mode only) --
  editCompanyManaged: boolean;
  onCompanyManagedChange: (value: boolean) => void;
  editSharedGroupIds: string[] | null;
  onSharedGroupIdsChange: (ids: string[] | null) => void;
  editSharedGroupPermissions?: Record<string, string> | null;
  onSharedGroupPermissionsChange?: (perms: Record<string, string> | null) => void;
  /** Entity type passed through to PrincipalSharingCard (e.g. "agent" for full permission set). */
  entityType?: string;
  /** Per-user grants + the company-managed access restriction.
   *  Supply from `useUserShareGrants(entity).props`. When omitted the card
   *  falls back to group-only sharing, which is what channel bots want. */
  userSharing?: UserSharingProps;
  /** Custom description for the company-managed toggle.
   *  When omitted a default "Shared with all users. Only admins can edit." is shown. */
  companyManagedDescription?: string;
  /** Optional extra content rendered right after the group-sharing picker and
   *  company-managed toggle (e.g. a contextual warning about the consequences
   *  of sharing this particular entity). */
  sharingNotice?: React.ReactNode;

  // -- Public catalog (edit mode, optional) --
  /** Current value for the public catalog toggle. When undefined the toggle is hidden. */
  editPublicCatalog?: boolean;
  /** Callback when the user toggles the public catalog switch. */
  onPublicCatalogChange?: (value: boolean) => void;

  // -- Owner transfer (admin only, optional) --
  /** When provided, an admin-only Owner picker is shown for any editable
   *  entity, including company-managed ones (which keep a real, reassignable
   *  owner). It is hidden for non-admins and for locked entities (gated on
   *  canEdit). Called with the selected user id to reassign the owner (saves
   *  immediately, independent of the edit/save flow). */
  onTransferOwner?: (userId: string) => void;

  // -- Content slots --
  /** Content rendered in read (non-editing) mode. */
  readView: React.ReactNode;
  /** Content rendered inside the cyan edit wrapper. */
  editView: React.ReactNode;
  /** Optional extra element rendered in the edit header row (e.g. Show/Hide secrets toggle). */
  editHeaderExtra?: React.ReactNode;
}

export function EntityConfigCard({
  entityLabel,
  isCompanyManaged,
  isBuiltin = false,
  isPackageOrigin = false,
  userId,
  sharedGroupIds,
  ownerName,
  canManage,
  hasManageRights = canManage,
  canEdit = true,
  editing,
  onStartEditing,
  onCancel,
  onSave,
  isSaving,
  requiresAi = true,
  canSave = true,
  editCompanyManaged,
  onCompanyManagedChange,
  editSharedGroupIds,
  onSharedGroupIdsChange,
  editSharedGroupPermissions,
  onSharedGroupPermissionsChange,
  entityType,
  userSharing,
  editPublicCatalog,
  onPublicCatalogChange,
  onTransferOwner,
  companyManagedDescription,
  sharingNotice,
  readView,
  editView,
  editHeaderExtra,
}: EntityConfigCardProps) {
  /* Saving an AI resource is refused with AI off (FR-018), so the
     control says so instead of failing on click. This card is shared by the
     prompt, skill, custom-tool, knowledge-base and MCP-server config tabs. */
  const aiDisabled = useAiActionDisabled();
  const aiOff = aiDisabled && requiresAi;
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const catalogEnabled = useAICatalogEnabled();
  // "Shared with me" covers either principal kind — a viewer reached by a
  // direct grant is just as much a guest on someone else's entity as a viewer
  // reached through a group, and should see the same banner.
  const isShared =
    !isCompanyManaged
    && !!userId
    && userId !== user?.id
    && (!!sharedGroupIds?.length || !!userSharing?.userIds?.length);
  // Why there is no Edit button, named by its real cause: a read-only share, a
  // company-managed entity, a built-in, or a lock the page explains itself
  // (whatever `canManage` / `canEdit` withhold beyond the viewer's own right).
  const lockReason = configLockReason({
    isShared,
    isCompanyManaged,
    isBuiltin,
    isPackageOrigin,
    canManage: hasManageRights,
    canEdit: canManage && canEdit,
  });
  const locked = lockReason !== null;
  const showEditButton = !editing && !locked;
  const showReadOnlyBadge = !editing && locked;

  return (
    <Card>
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <CardTitle className="content-base flex items-center gap-2">
          <Settings2 className="h-4 w-4 text-accent" />
          {t_i18n("Configuration")}
        </CardTitle>
        {showEditButton && (
          <Button variant="secondary" className="gap-1.5" onClick={onStartEditing}>
            <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit")}
          </Button>
        )}
        {showReadOnlyBadge && <ReadOnlyBadge size="sm" />}
      </CardHeader>
      <CardContent className="space-y-4">
        {!editing ? (
          <>
            {readView}
            {lockReason === "builtin" && (
              <CompanyManagedBanner entityLabel={entityLabel} isBuiltin />
            )}
            {lockReason === "package_origin" && (
              <CompanyManagedBanner entityLabel={entityLabel} isPackageOrigin />
            )}
            {lockReason === "company_managed" && (
              <CompanyManagedBanner entityLabel={entityLabel} />
            )}
            {isShared && (
              <SharedBanner entityLabel={entityLabel} ownerName={ownerName} readOnly={lockReason === "read_only_share"} />
            )}
          </>
        ) : (
          <div className="rounded-sm bg-accent/5 p-4 space-y-4">
            <div className="flex items-center justify-between">
              <span className="content-base font-medium">
                {/* No forced capitalization: casing rules differ across locales
                    (and some scripts have none), so the translated label is
                    passed through unchanged. */}
                {t_i18n("Edit {entity}", { entity: t_i18n(entityLabel) })}
              </span>
              {editHeaderExtra}
            </div>

            {editView}

            {/* Sharing. Now rendered for company-managed entities too:
                there the same picker names the audience that an access
                restriction narrows down to, instead of widening a private
                entity. A caller that has not wired per-user grants keeps the
                old behaviour — hidden when company-managed — because without
                them there is no restriction to configure. */}
            {(!editCompanyManaged || !!userSharing) && (
              <PrincipalSharingCard
                sharedGroupIds={editSharedGroupIds}
                onChange={onSharedGroupIdsChange}
                sharedGroupPermissions={editSharedGroupPermissions}
                onPermissionsChange={onSharedGroupPermissionsChange}
                userSharing={userSharing}
                entityType={entityType}
                isCompanyManaged={editCompanyManaged}
                disabled={isSaving || aiOff}
              />
            )}

            {/* Company-managed toggle (admin only) */}
            {isAdmin && (
              <CompanyManagedSwitch
                label={t_i18n("Company-managed {entity}", { entity: t_i18n(entityLabel) })}
                description={companyManagedDescription ? t_i18n(companyManagedDescription) : t_i18n("Shared with all users. Only admins can edit.")}
                checked={editCompanyManaged}
                onCheckedChange={(v) => {
                  onCompanyManagedChange(v);
                  // Going company-managed used to clear the grants, since
                  // "visible to everyone" made them redundant. With an access
                  // restriction available they may instead become the
                  // audience, so keep them for callers that can express one
                  // and let the restriction toggle decide. The
                  // backend applies the same rule on save.
                  if (v && !userSharing) {
                    onSharedGroupIdsChange(null);
                    onSharedGroupPermissionsChange?.(null);
                  }
                }}
              />
            )}

            {sharingNotice}

            {/* Public catalog toggle — visible to anyone who can manage the entity.
                For company-managed entities, canManage is admin-only by design. */}
            {catalogEnabled && editPublicCatalog !== undefined && onPublicCatalogChange && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                <div className="flex items-center gap-2">
                  <Globe className="h-4 w-4 text-accent" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Include in AI Catalog")}</p>
                    <p className="content-compact text-default-secondary">
                      {editPublicCatalog
                        ? t_i18n("This {entity} is visible in the AI Catalog.", { entity: t_i18n(entityLabel) })
                        : t_i18n("Publish this {entity} to the AI Catalog.", { entity: t_i18n(entityLabel) })}
                    </p>
                  </div>
                </div>
                <Switch aria-label={t_i18n("Include in AI Catalog")}
                  checked={editPublicCatalog}
                  onCheckedChange={onPublicCatalogChange}
                />
              </div>
            )}

            {/* Cancel then Save, at the right end, at the default 36 px button height (form standard). */}
            <div className="flex justify-end gap-2 pt-1" data-testid="entity-config-card.actions">
              <Button variant="secondary" onClick={onCancel}>{t_i18n("Cancel")}</Button>
              <AiDisabledTip>
              <Button onClick={onSave} disabled={!canSave || isSaving || aiOff}>
                {isSaving ? t_i18n("Saving...") : t_i18n("Save changes")}
              </Button>
              </AiDisabledTip>
            </div>
          </div>
        )}

        {/* Owner transfer (admin only). Saves immediately, independent of the
            edit/save flow above. Company-managed entities keep a real owner
            (always-set-owner invariant) that an admin can
            reassign, so the control is shown for them too (canManage is true
            for admins on company-managed entities). Gated on the same lock as
            the Edit button, so read-only / locked entities (e.g. system-managed
            KBs, built-ins) never expose it. */}
        {onTransferOwner && isAdmin && !locked && (
          <OwnerCard
            userId={userId}
            ownerName={ownerName}
            onTransfer={onTransferOwner}
            disabled={isSaving}
            entityLabel={entityLabel}
          />
        )}
      </CardContent>
    </Card>
  );
}
