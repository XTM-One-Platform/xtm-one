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
import { FileCode, Image, Lock } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useUpdateKnowledgeBase } from "@/hooks/api/useKnowledgeBaseApi";
import { useAuth } from "@/hooks/useAuth";
import { canManageEntity, formatDate } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { KnowledgeBase, KnowledgeBaseHtmlIngestion } from "@/types";

export function ConfigTab({ kb, canManage }: { kb: KnowledgeBase; canManage: boolean }) {
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const updateKB = useUpdateKnowledgeBase();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();
  const [name, setName] = useState(kb.name);
  const [description, setDescription] = useState(kb.description || "");
  const [kbTags, setKbTags] = useState<string[]>(kb.tags || []);
  const [editing, setEditing] = useState(false);
  const [companyManaged, setCompanyManaged] = useState(kb.is_company_managed);
  const [readOnly, setReadOnly] = useState(kb.is_read_only);
  const [renderHtml, setRenderHtml] = useState(kb.html_ingestion === "rendered");
  const [describeFigures, setDescribeFigures] = useState(!!kb.describe_figures);
  const [sharedGroupIds, setSharedGroupIds] = useState<string[] | null>(kb.shared_group_ids ?? null);
  const [sharedGroupPermissions, setSharedGroupPermissions] = useState<Record<string, string> | null>(kb.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(kb);

  const startEditing = () => {
    setEditing(true);
    setName(kb.name);
    setDescription(kb.description || "");
    setKbTags(kb.tags || []);
    setCompanyManaged(kb.is_company_managed);
    setReadOnly(kb.is_read_only);
    setRenderHtml(kb.html_ingestion === "rendered");
    setDescribeFigures(!!kb.describe_figures);
    setSharedGroupIds(kb.shared_group_ids ?? null);
    setSharedGroupPermissions(kb.shared_group_permissions ?? null);
    shareGrants.reset();
  };

  const handleSave = async () => {
    try {
      const groupsChanged = JSON.stringify(sharedGroupIds) !== JSON.stringify(kb.shared_group_ids ?? null);
      const permsChanged = JSON.stringify(sharedGroupPermissions) !== JSON.stringify(kb.shared_group_permissions ?? null);
      const grants = shareGrants.toPayload(companyManaged);
      const confirmed = await confirmSharing(
        {
          entity_type: "knowledge_base",
          entity_id: kb.id,
          ...(isAdmin && companyManaged !== kb.is_company_managed ? { is_company_managed: companyManaged } : {}),
          ...(groupsChanged ? { shared_group_ids: companyManaged ? null : sharedGroupIds } : {}),
          shared_user_ids: grants.shared_user_ids,
          access_restricted: grants.access_restricted,
        },
        { confirmLabel: t_i18n("Save and share"), unshareConfirmLabel: t_i18n("Save and stop sharing") },
      );
      if (!confirmed) return;
      const htmlIngestion: KnowledgeBaseHtmlIngestion = renderHtml ? "rendered" : "source";
      await updateKB.mutateAsync({
        id: kb.id,
        name,
        description: description || undefined,
        tags: kbTags,
        ...(readOnly !== kb.is_read_only ? { is_read_only: readOnly } : {}),
        ...(htmlIngestion !== kb.html_ingestion ? { html_ingestion: htmlIngestion } : {}),
        ...(describeFigures !== !!kb.describe_figures ? { describe_figures: describeFigures } : {}),
        ...(isAdmin && companyManaged !== kb.is_company_managed ? { is_company_managed: companyManaged } : {}),
        ...(groupsChanged ? { shared_group_ids: companyManaged ? null : (sharedGroupIds && sharedGroupIds.length > 0 ? sharedGroupIds : null) } : {}),
        ...(permsChanged ? { shared_group_permissions: companyManaged ? null : (sharedGroupPermissions && Object.keys(sharedGroupPermissions).length > 0 ? sharedGroupPermissions : null) } : {}),
        // Always sent, unlike the group fields above: this page only forwards
        // group changes it can detect as different, and there is no stored
        // baseline to diff a brand-new grant against.
        ...grants,
      });
      toast({ title: t_i18n("Configuration saved"), variant: "success" });
      setEditing(false);
    } catch {
      toast({ title: t_i18n("Failed to save"), variant: "error" });
    }
  };

  return (
    <div className="space-y-6">
      {confirmSharingElement}
      <EntityConfigCard
        entityLabel="knowledge base"
        isCompanyManaged={kb.is_company_managed}
        isBuiltin={kb.is_builtin}
        isPackageOrigin={!!kb.is_package_origin}
        userId={kb.user_id}
        sharedGroupIds={kb.shared_group_ids}
        ownerName={kb.owner_name}
        canManage={canManage}
        hasManageRights={canManageEntity(kb, user?.id, isAdmin)}
        canEdit={!kb.is_system_managed}
        editing={editing}
        onStartEditing={startEditing}
        onCancel={() => setEditing(false)}
        onSave={handleSave}
        isSaving={updateKB.isPending}
        editCompanyManaged={companyManaged}
        onCompanyManagedChange={setCompanyManaged}
        editSharedGroupIds={sharedGroupIds}
        onSharedGroupIdsChange={setSharedGroupIds}
        editSharedGroupPermissions={sharedGroupPermissions}
        onSharedGroupPermissionsChange={setSharedGroupPermissions}
        userSharing={shareGrants.props}
        onTransferOwner={async (userId) => {
          if (!(await confirmSharing({ entity_type: "knowledge_base", entity_id: kb.id, new_owner_id: userId }, { confirmLabel: t_i18n("Transfer") }))) return;
          updateKB.mutate(
            { id: kb.id, user_id: userId },
            { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
          );
        }}
        companyManagedDescription={
          companyManaged
            ? t_i18n("Shared with all users. Toggling off will make it private and remove it from other users' agents.")
            : t_i18n("Private to you. Toggle on to share with all users.")
        }
        readView={
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Name")}</label><p className="content-base">{kb.name}</p></div>
              <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Created")}</label><p className="content-compact text-default-secondary">{formatDate(kb.created_at)}</p></div>
            </div>
            {kb.description && (
              <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Description")}</label><p className="content-base text-default-secondary">{kb.description}</p></div>
            )}
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Content")}</label>
              <p className="content-base flex items-center gap-1.5">
                {kb.is_read_only && <Lock className="h-3 w-3 text-feedback-warning-primary" />}
                {kb.is_read_only ? t_i18n("Read-only: locked as ground truth") : t_i18n("Editable")}
              </p>
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("HTML documents")}</label>
              <p className="content-base flex items-center gap-1.5">
                <FileCode className="h-3 w-3 text-default-secondary" />
                {kb.html_ingestion === "rendered" ? t_i18n("Indexed as rendered page text") : t_i18n("Indexed as HTML source")}
              </p>
              <p className="content-caption text-default-secondary">{t_i18n("Applies to each document when it is indexed: documents indexed before a change keep their previous indexing until they are re-embedded.")}</p>
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("PDF figures")}</label>
              <p className="content-base flex items-center gap-1.5">
                <Image className="h-3 w-3 text-default-secondary" />
                {kb.describe_figures ? t_i18n("Described by a vision model and indexed with the text") : t_i18n("Not described: only the text layer is indexed")}
              </p>
            </div>
            {kb.is_read_only && (
              <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                <p className="content-compact text-feedback-warning-primary">{t_i18n("This knowledge base is read-only. It stays fully searchable, but agents cannot write to it and its documents cannot be added, edited or deleted. Configured import sources still refresh it: disable them too to freeze it completely.")}</p>
              </div>
            )}
            {kb.is_system_managed && (
              <div className="rounded-sm bg-accent/5 p-3">
                <p className="content-compact text-accent">{t_i18n("This knowledge base is system-managed. It was auto-created for an agent and its content is managed by assignment executions. Configuration cannot be edited.")}</p>
              </div>
            )}
          </>
        }
        editView={
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Name")}</label><Input aria-label={t_i18n("Name")} value={name} onChange={(e) => setName(e.target.value)} className="h-8 content-base" /></div>
              <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Tags")}</label><TagInput value={kbTags} onChange={setKbTags} placeholder={t_i18n("Add tags…")} /></div>
            </div>
            <div className="space-y-1"><label className="content-compact text-default-secondary">{t_i18n("Description")}</label><Input aria-label={t_i18n("Description")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("Description (optional)")} className="h-8 content-base" /></div>
            <div className="flex items-start justify-between gap-4 rounded-sm border border-elevation-subtle-soft p-3">
              <div>
                <p className="content-compact font-medium flex items-center gap-1.5"><Lock className="h-3 w-3" />{t_i18n("Read-only content")}</p>
                <p className="content-caption text-default-secondary mt-0.5">
                  {readOnly
                    ? t_i18n("Locked as ground truth. Still fully searchable, but agents cannot write to it and documents cannot be added, edited or deleted. Configured import sources keep refreshing it.")
                    : t_i18n("Turn on to treat this collection as ground truth: agents lose write access to it and its documents can no longer be added, edited or deleted.")}
                </p>
              </div>
              <Switch checked={readOnly} onCheckedChange={setReadOnly} aria-label={t_i18n("Read-only content")} />
            </div>
            <div className="flex items-start justify-between gap-4 rounded-sm border border-elevation-subtle-soft p-3">
              <div>
                <p className="content-compact font-medium flex items-center gap-1.5"><FileCode className="h-3 w-3" />{t_i18n("Index HTML as rendered text")}</p>
                <p className="content-caption text-default-secondary mt-0.5">
                  {renderHtml
                    ? t_i18n("HTML documents are embedded as their page text in reading order, with the data they embed summarised first and the markup left out. The stored documents do not change. Applies to documents added or updated from now on; use Re-embed to apply it to the existing ones.")
                    : t_i18n("HTML documents are embedded as their source markup. Turn on to embed their rendered page text instead, which suits exported pages whose markup outweighs their text.")}
                </p>
              </div>
              <Switch checked={renderHtml} onCheckedChange={setRenderHtml} aria-label={t_i18n("Index HTML as rendered text")} />
            </div>
            <div className="flex items-start justify-between gap-4 rounded-sm border border-elevation-subtle-soft p-3">
              <div>
                <p className="content-compact font-medium flex items-center gap-1.5"><Image className="h-3 w-3" />{t_i18n("Describe PDF figures")}</p>
                <p className="content-caption text-default-secondary mt-0.5">
                  {describeFigures
                    ? t_i18n("Pages of an uploaded PDF that carry diagrams, charts, screenshots or no text layer are read by the platform's vision model, and each description is indexed next to its page's text. Each such page costs one model call, up to 30 pages per document. Applies to PDFs uploaded from now on.")
                    : t_i18n("Only the text layer of a PDF is indexed: what it shows as images or drawings is not. Turn on to describe its figures at upload with the platform's vision model.")}
                </p>
              </div>
              <Switch checked={describeFigures} onCheckedChange={setDescribeFigures} aria-label={t_i18n("Describe PDF figures")} />
            </div>
          </>
        }
      />
    </div>
  );
}
