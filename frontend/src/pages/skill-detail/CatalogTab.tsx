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

import { useState } from "react";
import { Scale } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useSkillLicense, useUpdateSkill } from "@/hooks/api/useSkillApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { AgentSkill } from "@/types";
import { NO_SOURCE_CREDENTIAL, PrivateSourceFields } from "./PrivateSourceFields";

function CatalogCard({ skill, canManage }: { skill: AgentSkill; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateSkill();
  const { data: integrations } = useIntegrations({ scope: "all" });
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();

  const [editName, setEditName] = useState(skill.name);
  const [editDescription, setEditDescription] = useState(skill.description ?? "");
  const [editUrl, setEditUrl] = useState(skill.url ?? "");
  const [editSourceIntegrationId, setEditSourceIntegrationId] = useState(
    skill.source_integration_id ?? NO_SOURCE_CREDENTIAL,
  );
  const [editSourceRef, setEditSourceRef] = useState(skill.source_ref ?? "");
  const [editCompatibility, setEditCompatibility] = useState(skill.compatibility ?? "");
  const [editLicense, setEditLicense] = useState(skill.license ?? "");
  const [editTags, setEditTags] = useState<string[]>(skill.tags || []);
  const [editEnabled, setEditEnabled] = useState(skill.enabled);
  const [editHideFromChat, setEditHideFromChat] = useState(skill.hide_from_chat);
  const [editCompanyManaged, setEditCompanyManaged] = useState(skill.is_company_managed);
  const [editPublicCatalog, setEditPublicCatalog] = useState(skill.is_public_catalog ?? false);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(skill.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(skill.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(skill);

  const startEditing = () => {
    setEditing(true);
    setEditName(skill.name);
    setEditDescription(skill.description ?? "");
    setEditUrl(skill.url ?? "");
    setEditSourceIntegrationId(skill.source_integration_id ?? NO_SOURCE_CREDENTIAL);
    setEditSourceRef(skill.source_ref ?? "");
    setEditCompatibility(skill.compatibility ?? "");
    setEditLicense(skill.license ?? "");
    setEditTags(skill.tags || []);
    setEditEnabled(skill.enabled);
    setEditHideFromChat(skill.hide_from_chat);
    setEditCompanyManaged(skill.is_company_managed);
    setEditPublicCatalog(skill.is_public_catalog ?? false);
    setEditSharedGroupIds(skill.shared_group_ids ?? null);
    setEditSharedGroupPermissions(skill.shared_group_permissions ?? null);
    shareGrants.reset();
  };

  const handleSave = async () => {
    if (!editName.trim()) return;
    const resolvedGroupIds = editCompanyManaged ? undefined : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : undefined);
    const resolvedGroupPermissions = editCompanyManaged ? null : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);
    // `null` detaches the credential; a tracked branch only applies with one.
    const resolvedCredentialId = editSourceIntegrationId !== NO_SOURCE_CREDENTIAL ? editSourceIntegrationId : null;
    const grants = shareGrants.toPayload(editCompanyManaged);
    // Asked whether or not a credential is bound: sharing the skill also lends
    // the variables its content references.
    if (!(await confirmSharing(
      {
        entity_type: "skill",
        entity_id: skill.id,
        integration_ids: resolvedCredentialId ? [resolvedCredentialId] : [],
        is_company_managed: editCompanyManaged,
        shared_group_ids: resolvedGroupIds ?? null,
        shared_user_ids: grants.shared_user_ids,
        access_restricted: grants.access_restricted,
      },
      { confirmLabel: t_i18n("Save and share"), unshareConfirmLabel: t_i18n("Save and stop sharing") },
    ))) {
      return;
    }
    update.mutate(
      {
        id: skill.id,
        name: editName,
        description: editDescription || undefined,
        url: editUrl || undefined,
        source_integration_id: resolvedCredentialId,
        source_ref: resolvedCredentialId ? (editSourceRef.trim() || null) : null,
        compatibility: editCompatibility || undefined,
        // Sent even empty: a blank licence clears it.
        license: editLicense,
        tags: editTags,
        enabled: editEnabled,
        hide_from_chat: editHideFromChat,
        is_company_managed: editCompanyManaged,
        is_public_catalog: editPublicCatalog,
        shared_group_ids: resolvedGroupIds,
        shared_group_permissions: resolvedGroupPermissions,
        ...grants,
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Catalog updated"), description: t_i18n("Skill metadata saved successfully."), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  return (
    <>
    {confirmSharingElement}
    <EntityConfigCard
      entityLabel="skill catalog"
      isCompanyManaged={skill.is_company_managed}
      isBuiltin={skill.tags?.includes("builtin") ?? false}
      userId={skill.user_id}
      sharedGroupIds={skill.shared_group_ids}
      ownerName={skill.owner_name}
      canManage={canManage}
      editing={editing}
      onStartEditing={startEditing}
      onCancel={() => setEditing(false)}
      onSave={handleSave}
      isSaving={update.isPending}
      canSave={!!editName.trim()}
      editCompanyManaged={editCompanyManaged}
      onCompanyManagedChange={setEditCompanyManaged}
      editPublicCatalog={editPublicCatalog}
      onPublicCatalogChange={setEditPublicCatalog}
      editSharedGroupIds={editSharedGroupIds}
      onSharedGroupIdsChange={setEditSharedGroupIds}
      editSharedGroupPermissions={editSharedGroupPermissions}
      onSharedGroupPermissionsChange={setEditSharedGroupPermissions}
      userSharing={shareGrants.props}
      onTransferOwner={async (userId) => {
        if (!(await confirmSharing({ entity_type: "skill", entity_id: skill.id, change: { new_owner_id: userId } }, { confirmLabel: t_i18n("Transfer") }))) return;
        update.mutate(
          { id: skill.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        );
      }}
      readView={(
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
              <p className="content-base font-medium">{skill.name}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Enabled")}</div>
              <Badge variant={skill.enabled ? "success" : "outline"} className="content-caption">
                {skill.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
              </Badge>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Hide from chat")}</div>
              <p className="content-base">{skill.hide_from_chat ? t_i18n("Yes") : t_i18n("No")}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Compatibility")}</div>
              <p className="content-base">{skill.compatibility || "—"}</p>
            </div>
          </div>
          <div>
            <div className="content-compact text-default-secondary mb-1">{t_i18n("URL")}</div>
            <p className="content-base font-mono-plex truncate">{skill.url || "—"}</p>
          </div>
          {skill.source_integration_id && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Private repository access")}</div>
                <p className="content-base">
                  {integrations?.find((i) => i.id === skill.source_integration_id)?.name
                    ?? t_i18n("GitHub integration no longer available")}
                </p>
              </div>
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Tracked branch")}</div>
                <p className="content-base font-mono-plex">{skill.source_ref || t_i18n("Default branch")}</p>
              </div>
            </div>
          )}
          {skill.description && (
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Description")}</div>
              <p className="content-base text-default-secondary">{skill.description}</p>
            </div>
          )}
          {skill.tags && skill.tags.length > 0 && (
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Tags")}</div>
              <div className="flex flex-wrap gap-1">
                {skill.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
                ))}
              </div>
            </div>
          )}
        </>
      )}
      editView={(
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Name")}</label>
              <Input aria-label={t_i18n("Name")} value={editName} onChange={(e) => setEditName(e.target.value)} className="h-8 content-base" />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Compatibility")}</label>
              <Input aria-label={t_i18n("Compatibility")} value={editCompatibility} onChange={(e) => setEditCompatibility(e.target.value)} className="h-8 content-base" placeholder={t_i18n("e.g. xtm-one")} />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("URL (optional)")}</label>
              <Input aria-label={t_i18n("URL (optional)")} value={editUrl} onChange={(e) => setEditUrl(e.target.value)} className="h-8 content-base font-mono-plex" placeholder="https://..." />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Tags")}</label>
              <TagInput value={editTags} onChange={setEditTags} placeholder={t_i18n("Add tags…")} />
            </div>
          </div>

          {editUrl.trim() && (
            <PrivateSourceFields
              compact
              integrationId={editSourceIntegrationId}
              onIntegrationIdChange={setEditSourceIntegrationId}
              sourceRef={editSourceRef}
              onSourceRefChange={setEditSourceRef}
            />
          )}

          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("License")}</label>
            <Input aria-label={t_i18n("License")}
              value={editLicense}
              onChange={(e) => setEditLicense(e.target.value)}
              className="h-8 content-base"
              placeholder={t_i18n("e.g. MIT License. Copyright (c) 2026 Example Ltd.")}
            />
          </div>

          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Description")}</label>
            <textarea
              className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
              placeholder={t_i18n("Describe what this skill teaches the agent.")}
            />
          </div>

          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Enabled")}</p>
              <p className="text-[11px] text-default-secondary">{t_i18n("When disabled, this skill is not injected into agent prompts.")}</p>
            </div>
            <Switch aria-label={t_i18n("Enabled")} checked={editEnabled} onCheckedChange={setEditEnabled} />
          </div>

          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Hide from chat")}</p>
              <p className="text-[11px] text-default-secondary">{t_i18n("When enabled, this skill won't appear in chat contexts. It remains available for agent configuration.")}</p>
            </div>
            <Switch aria-label={t_i18n("Hide from chat")} checked={editHideFromChat} onCheckedChange={setEditHideFromChat} />
          </div>
        </>
      )}
    />
    </>
  );
}

/**
 * The skill's licence and the attribution its authors require: metadata about
 * the skill, kept out of the instructions the agent reads (where a copyright
 * line and its link would read like a task). The notice is the frontmatter
 * `license`; the full text is the `LICENSE.md` the skill bundles at its root.
 */
function LicenseCard({ skill }: { skill: AgentSkill }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useSkillLicense(skill.id);
  const notice = data?.license ?? skill.license;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Scale className="h-4 w-4 text-default-secondary" />
          {t_i18n("License and attribution")}
        </CardTitle>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("The license this skill is distributed under and the attribution its authors require. It is metadata: agents do not read it.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {notice ? (
          <p className="content-base text-default-secondary whitespace-pre-wrap break-words">{notice}</p>
        ) : (
          <p className="content-base text-default-secondary">{t_i18n("No license declared for this skill.")}</p>
        )}
        {!isLoading && data?.text && data.file_path && (
          <details className="rounded-sm border border-elevation-subtle-soft">
            <DisclosureSummary size="block" className="px-3 py-2 content-compact text-default-secondary">
              {t_i18n("Full license text ({path})", { path: data.file_path })}
            </DisclosureSummary>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words px-3 pb-3 content-compact text-default-secondary">
              {data.text}
            </pre>
            {data.truncated && (
              <p className="px-3 pb-3 text-[11px] text-default-secondary">{t_i18n("The license text is shortened here.")}</p>
            )}
          </details>
        )}
      </CardContent>
    </Card>
  );
}

export function CatalogTab({ skill, canManage }: { skill: AgentSkill; canManage: boolean }) {
  return (
    <div className="space-y-6">
      <CatalogCard skill={skill} canManage={canManage} />
      <LicenseCard skill={skill} />
    </div>
  );
}
