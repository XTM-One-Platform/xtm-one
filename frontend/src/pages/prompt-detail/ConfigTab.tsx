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

import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { pickSharingChange } from "@/lib/credential-exposure";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { useUpdatePrompt, usePromptVersions } from "@/hooks/api/usePromptApi";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { Prompt } from "@/types";

// ---------------------------------------------------------------------------
// Configuration Card
// ---------------------------------------------------------------------------

function ConfigurationCard({ prompt, canManage }: { prompt: Prompt; canManage: boolean }) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const update = useUpdatePrompt();
  // Only the active version badge is read from this query, so a single-
  // row fetch is sufficient: the list is ordered by newest first and the
  // active row (``is_active``) is therefore either at position 0 or
  // absent (no active version yet).  Fetching ``limit: 50`` here would
  // ship up to 50 PromptVersion rows on every render of the config tab.
  const { data: versionsData } = usePromptVersions(prompt.id, { limit: 1, offset: 0 });
  const activeVersionNumber = versionsData?.items.find((v) => v.is_active)?.version_number;
  const { toast } = useToast();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const [editing, setEditing] = useState(false);

  const [editName, setEditName] = useState(prompt.name);
  const [editDescription, setEditDescription] = useState(prompt.description ?? "");
  const [editContent, setEditContent] = useState(prompt.content ?? "");
  const [editTags, setEditTags] = useState<string[]>(prompt.tags || []);
  const [editHideFromChat, setEditHideFromChat] = useState(prompt.hide_from_chat);
  const [editCompanyManaged, setEditCompanyManaged] = useState(prompt.is_company_managed);
  const [editPublicCatalog, setEditPublicCatalog] = useState(prompt.is_public_catalog ?? false);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(prompt.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(prompt.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(prompt);
  const [editCommitMessage, setEditCommitMessage] = useState("");
  const contentRef = useRef<HTMLTextAreaElement>(null);

  const startEditing = () => {
    setEditing(true);
    setEditName(prompt.name);
    setEditDescription(prompt.description ?? "");
    setEditContent(prompt.content ?? "");
    setEditTags(prompt.tags || []);
    setEditHideFromChat(prompt.hide_from_chat);
    setEditCompanyManaged(prompt.is_company_managed);
    setEditPublicCatalog(prompt.is_public_catalog ?? false);
    setEditSharedGroupIds(prompt.shared_group_ids ?? null);
    setEditSharedGroupPermissions(prompt.shared_group_permissions ?? null);
    shareGrants.reset();
    setEditCommitMessage("");
  };

  const handleSave = async () => {
    if (!editName.trim()) return;
    const contentChanged = (editContent || "") !== (prompt.content || "");
    const resolvedGroupIds = editCompanyManaged ? null : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : null);
    const resolvedGroupPermissions = editCompanyManaged ? null : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);
    // A prompt lends the variables it references to whoever it reaches.
    if (!(await confirmSharing({
      entity_type: "prompt",
      entity_id: prompt.id,
      change: {
        ...pickSharingChange({
          is_company_managed: editCompanyManaged,
          shared_group_ids: resolvedGroupIds,
          shared_group_permissions: resolvedGroupPermissions,
          ...shareGrants.toPayload(editCompanyManaged),
        }),
        content: editContent,
      },
    }))) return;
    update.mutate(
      {
        id: prompt.id,
        name: editName,
        description: editDescription,
        content: editContent,
        tags: editTags,
        hide_from_chat: editHideFromChat,
        is_company_managed: editCompanyManaged,
        is_public_catalog: editPublicCatalog,
        commit_message: contentChanged && editCommitMessage ? editCommitMessage : undefined,
        shared_group_ids: resolvedGroupIds,
        shared_group_permissions: resolvedGroupPermissions,
        ...shareGrants.toPayload(editCompanyManaged),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Prompt updated"), description: t_i18n("Configuration saved successfully."), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  return (
    <>
      {consentElement}
    <EntityConfigCard
      entityLabel="prompt"
      isCompanyManaged={prompt.is_company_managed}
      userId={prompt.user_id}
      sharedGroupIds={prompt.shared_group_ids}
      ownerName={prompt.owner_name}
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
        if (!(await confirmSharing({ entity_type: "prompt", entity_id: prompt.id, change: { new_owner_id: userId } }, { confirmLabel: t_i18n("Transfer") }))) return;
        update.mutate(
          { id: prompt.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        );
      }}
      readView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
              <p className="content-base font-medium">{prompt.name}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Description")}</div>
              <p className="content-base">{prompt.description || "—"}</p>
            </div>
          </div>
          {prompt.content && (
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <div className="content-compact text-default-secondary">{t_i18n("Content")}</div>
                {activeVersionNumber !== undefined && (
                  <button
                    type="button"
                    onClick={() => navigate(`/prompts/${prompt.id}/versioning`)}
                    className="rounded-sm"
                    aria-label="Open prompt versioning"
                  >
                    <Badge variant="outline" className="h-4 px-1.5 content-caption border-elevation-subtle-soft text-default-secondary hover:border-accent/40 hover:text-accent transition-colors cursor-pointer">
                      v{activeVersionNumber}
                    </Badge>
                  </button>
                )}
              </div>
              <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact text-accent overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap">
                {prompt.content}
              </pre>
            </div>
          )}
        </>
      }
      editView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Prompt Name")}</label>
              <Input aria-label={t_i18n("Prompt Name")} value={editName} onChange={(e) => setEditName(e.target.value)} className="h-8 content-base" />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Tags")}</label>
              <TagInput value={editTags} onChange={setEditTags} placeholder={t_i18n("Add tags…")} />
            </div>
          </div>

          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Description")}</label>
            <textarea
              className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
            />
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <label className="content-compact text-default-secondary">{t_i18n("Content")}</label>
                {activeVersionNumber !== undefined && (
                  <button
                    type="button"
                    onClick={() => navigate(`/prompts/${prompt.id}/versioning`)}
                    className="rounded-sm"
                    aria-label="Open prompt versioning"
                  >
                    <Badge variant="outline" className="h-4 px-1.5 content-caption border-elevation-subtle-soft text-default-secondary hover:border-accent/40 hover:text-accent transition-colors cursor-pointer">
                      v{activeVersionNumber}
                    </Badge>
                  </button>
                )}
              </div>
              <VariableInsertButton textareaRef={contentRef} value={editContent} onChange={setEditContent} />
            </div>
            <textarea
              ref={contentRef}
              className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
            />
          </div>

          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Commit message for content change (optional)")}</label>
            <Input aria-label={t_i18n("Describe this prompt change")}
              value={editCommitMessage}
              onChange={(e) => setEditCommitMessage(e.target.value)}
              className="h-8 content-base"
              placeholder={t_i18n("Describe this prompt change")}
            />
          </div>

          {/* Hide from chat toggle */}
          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Hide from chat")}</p>
              <p className="text-[11px] text-default-secondary">{t_i18n("When enabled, this prompt won't appear in the chat prompt picker. It will still be available for agent configuration.")}</p>
            </div>
            <Switch aria-label={t_i18n("Hide from chat")} checked={editHideFromChat} onCheckedChange={setEditHideFromChat} />
          </div>
        </>
      }
    />
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab: Configuration
// ---------------------------------------------------------------------------

export function ConfigTab({ prompt, canManage }: { prompt: Prompt; canManage: boolean }) {
  return (
    <div className="space-y-6">
      <ConfigurationCard prompt={prompt} canManage={canManage} />
    </div>
  );
}
