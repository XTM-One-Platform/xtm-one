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

import { useState, useRef } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { useApprovalHolders } from "@/hooks/api/useToolApprovals";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { pickSharingChange } from "@/lib/credential-exposure";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { useUpdateCustomTool } from "@/hooks/api/useCustomToolApi";
import type { CustomTool, CustomToolLanguage } from "@/types";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { parseSchemaProps, buildSchema, newSchemaRow, toSchemaRows, PARAM_TYPE_OPTIONS } from "./schema-utils";
import type { SchemaProperty, SchemaPropertyRow } from "./schema-utils";
import { CustomToolCodeGenerator } from "./CustomToolCodeGenerator";

// ---------------------------------------------------------------------------
// Configuration Card
// ---------------------------------------------------------------------------

/**
 * What editing the parameters does to consent already given.
 *
 * Counted rather than described. The warning began as "anyone who chose always
 * allow will be asked again", which is true and easy to skim past; the number
 * is what makes it a decision. Zero says so too — that a change costs nobody
 * anything is worth knowing before making it.
 */
function ParameterConsentNotice({ tool }: { tool: CustomTool }) {
  const { t_i18n } = useFormatter();
  const { data } = useApprovalHolders(tool.name);
  const others = data?.others;
  return (
    <p className="text-[11px] text-default-secondary">
      {others === undefined
        ? t_i18n(
            "Changing these withdraws any “always allow” given for this tool.",
          )
        : others === 0
        ? t_i18n(
            "Nobody else has chosen “always allow” for this tool, so changing these affects no one but you.",
          )
        : t_i18n(
            "{n, plural, one {# other person has} other {# other people have}} chosen “always allow” for this tool. Changing these makes them be asked again.",
            { n: others },
          )}
    </p>
  );
}

function ConfigurationCard({ tool, canManage }: { tool: CustomTool; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateCustomTool();
  const { toast } = useToast();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const [editing, setEditing] = useState(false);
  // Synced from a repository: the content (name, code, parameters, timeout)
  // changes there; sharing and the chat toggle stay editable here.
  const synced = !!tool.source_id;

  const [editName, setEditName] = useState(tool.name);
  const [editDescription, setEditDescription] = useState(tool.description ?? "");
  const [editIconUrl, setEditIconUrl] = useState(tool.icon_url || "");
  const [editTags, setEditTags] = useState<string[]>(tool.tags || []);
  const [editJsFunction, setEditJsFunction] = useState(tool.js_function || "");
  const [editLanguage, setEditLanguage] = useState<CustomToolLanguage>(tool.language || "javascript");
  const [editTimeout, setEditTimeout] = useState(tool.timeout ?? 300);
  const [editHideFromChat, setEditHideFromChat] = useState(tool.hide_from_chat);
  const [editCompanyManaged, setEditCompanyManaged] = useState(tool.is_company_managed);
  const [editPublicCatalog, setEditPublicCatalog] = useState(tool.is_public_catalog ?? false);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(tool.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(tool.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(tool);
  const [editProperties, setEditProperties] = useState<SchemaPropertyRow[]>(
    () => toSchemaRows(parseSchemaProps(tool.input_schema)),
  );

  const startEditing = () => {
    setEditing(true);
    setEditName(tool.name);
    setEditDescription(tool.description ?? "");
    setEditIconUrl(tool.icon_url || "");
    setEditJsFunction(tool.js_function || "");
    setEditLanguage(tool.language || "javascript");
    setEditTimeout(tool.timeout ?? 300);
    setEditHideFromChat(tool.hide_from_chat);
    setEditCompanyManaged(tool.is_company_managed);
    setEditPublicCatalog(tool.is_public_catalog ?? false);
    setEditSharedGroupIds(tool.shared_group_ids ?? null);
    setEditSharedGroupPermissions(tool.shared_group_permissions ?? null);
    shareGrants.reset();
    setEditProperties(toSchemaRows(parseSchemaProps(tool.input_schema)));
  };

  const handleSave = async () => {
    if (!editName.trim()) return;
    const validProps = editProperties.filter((p) => p.name.trim());
    const resolvedGroupIds = editCompanyManaged ? null : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : null);
    const resolvedGroupPermissions = editCompanyManaged ? null : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);
    const sharing = {
      is_company_managed: editCompanyManaged,
      shared_group_ids: resolvedGroupIds,
      shared_group_permissions: resolvedGroupPermissions,
      ...shareGrants.toPayload(editCompanyManaged),
    };
    // The tool's code, and the variables it references, reach whoever it is
    // shared with: said before it is saved.
    if (!(await confirmSharing({
      entity_type: "custom_tool",
      entity_id: tool.id,
      change: synced
        ? pickSharingChange(sharing)
        : { ...pickSharingChange(sharing), js_function: editJsFunction, description: editDescription },
    }))) return;
    const content = synced
      ? {}
      : {
          name: editName,
          description: editDescription,
          icon_url: editIconUrl || undefined,
          tags: editTags,
          input_schema: validProps.length > 0 ? buildSchema(validProps) : undefined,
          js_function: editJsFunction || undefined,
          language: editLanguage,
          timeout: editTimeout,
        };
    update.mutate(
      {
        id: tool.id,
        ...content,
        hide_from_chat: editHideFromChat,
        // The source decides the scope of the tools it syncs.
        ...(synced ? {} : { is_company_managed: editCompanyManaged }),
        is_public_catalog: editPublicCatalog,
        shared_group_ids: resolvedGroupIds,
        shared_group_permissions: resolvedGroupPermissions,
        ...shareGrants.toPayload(editCompanyManaged),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Tool updated"), description: t_i18n("Configuration saved successfully."), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  const jsRef = useRef<HTMLTextAreaElement>(null);
  const addProperty = () => setEditProperties([...editProperties, newSchemaRow()]);
  const removeProperty = (idx: number) => setEditProperties(editProperties.filter((_, i) => i !== idx));
  const updateProperty = (idx: number, field: keyof SchemaProperty, value: string | boolean) => {
    const next = [...editProperties];
    next[idx] = { ...next[idx], [field]: value };
    setEditProperties(next);
  };

  return (
    <>
      {consentElement}
    <EntityConfigCard
      /* A Custom tool is a sandboxed script: editing it calls no provider,
         and a deterministic flow runs it via ``action.script``. */
      requiresAi={false}
      entityLabel="tool"
      isCompanyManaged={tool.is_company_managed}
      isPackageOrigin={!!tool.is_package_origin}
      userId={tool.user_id}
      sharedGroupIds={tool.shared_group_ids}
      ownerName={tool.owner_name}
      canManage={canManage}
      editing={editing}
      onStartEditing={startEditing}
      onCancel={() => setEditing(false)}
      onSave={handleSave}
      isSaving={update.isPending}
      canSave={!!editName.trim()}
      editCompanyManaged={editCompanyManaged}
      /* A synced tool's scope is its source's: the switch shows it, unchangeable. */
      onCompanyManagedChange={synced ? () => undefined : setEditCompanyManaged}
      companyManagedDescription={synced ? "Set by the repository source this tool is synced from." : undefined}
      editPublicCatalog={editPublicCatalog}
      onPublicCatalogChange={setEditPublicCatalog}
      editSharedGroupIds={editSharedGroupIds}
      onSharedGroupIdsChange={setEditSharedGroupIds}
      editSharedGroupPermissions={editSharedGroupPermissions}
      onSharedGroupPermissionsChange={setEditSharedGroupPermissions}
      userSharing={shareGrants.props}
      onTransferOwner={async (userId) => {
        if (!(await confirmSharing({ entity_type: "custom_tool", entity_id: tool.id, change: { new_owner_id: userId } }, { confirmLabel: t_i18n("Transfer") }))) return;
        update.mutate(
          { id: tool.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        );
      }}
      readView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
              <p className="content-base font-medium font-mono-plex">{tool.name}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Icon URL")}</div>
              <p className="content-base font-mono-plex truncate">{tool.icon_url || "—"}</p>
            </div>
          </div>
          {tool.js_function && (
            <div>
              <div className="content-compact text-default-secondary mb-1 flex items-center gap-2">
                {tool.language === "python" ? t_i18n("Python Function") : t_i18n("JavaScript Function")}
                <span className={cn(
                  "rounded-sm px-1.5 py-0.5 text-[9px] font-medium",
                  tool.language === "python" ? "bg-feedback-info-primary/10 text-feedback-info-primary" : "bg-feedback-alert-primary/10 text-feedback-warning-tertiary",
                )}>
                  {tool.language === "python" ? "Python" : "JS"}
                </span>
              </div>
              <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact font-mono-plex text-accent overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap break-all">
                {tool.js_function}
              </pre>
            </div>
          )}
        </>
      }
      editView={
        <>
          {synced && (
            <p className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary">
              {t_i18n("This tool is synced from the repository source {source}: its name, code, parameters and tests change in the repository. Detach it to edit them here.", { source: tool.source_name ?? "" })}
            </p>
          )}
          {!synced && (<>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Tool Name")}</label>
              <Input aria-label={t_i18n("Tool Name")} value={editName} onChange={(e) => setEditName(e.target.value)} className="h-8 content-base font-mono-plex" />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Icon URL")}</label>
              <Input aria-label={t_i18n("Icon URL")} value={editIconUrl} onChange={(e) => setEditIconUrl(e.target.value)} className="h-8 content-base" />
            </div>
            <div className="space-y-1 sm:col-span-2 xl:col-span-1">
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

          {/* Editable parameters */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="content-compact text-default-secondary font-medium">{t_i18n("Input Parameters")}</label>
              <Button variant="ghost" size="sm" className="h-6 gap-1 content-compact text-default-secondary" onClick={addProperty}>
                <Icon name="plus" size={16} /> {t_i18n("Add")}
              </Button>
            </div>
            {/* Standing approvals are fingerprinted against this schema, so
                changing it withdraws every "always allow" anyone gave this
                tool. That is the correct behaviour — consent was to the old
                shape — but it happened silently, and the person changing the
                parameters is the only one who can say so in advance. */}
            <ParameterConsentNotice tool={tool} />
            {editProperties.map((prop, idx) => (
              <div key={prop.rowId} className="grid grid-cols-[1fr_80px_1fr_60px_30px] gap-2 items-center">
                <Input aria-label={t_i18n("Name")} value={prop.name} onChange={(e) => updateProperty(idx, "name", e.target.value)} placeholder="name" className="h-7 content-compact font-mono-plex" />
                <select aria-label={t_i18n("Type")} value={prop.type} onChange={(e) => updateProperty(idx, "type", e.target.value)} className="h-7 rounded-sm border border-elevation-subtle bg-elevation-default px-1 content-compact">
                  {PARAM_TYPE_OPTIONS.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                <Input aria-label={t_i18n("description")} value={prop.description} onChange={(e) => updateProperty(idx, "description", e.target.value)} placeholder={t_i18n("description")} className="h-7 content-compact" />
                <Switch aria-label={t_i18n("Required")} checked={prop.required} onCheckedChange={(v) => updateProperty(idx, "required", v)} />
                <Button aria-label={t_i18n("Remove parameter")} variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => removeProperty(idx)}>
                  <Icon name="minus" size={16} className="text-default-secondary" />
                </Button>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="content-compact text-default-secondary">{t_i18n("Language")}</label>
              <div className="flex rounded-sm border border-elevation-subtle-soft overflow-hidden">
                {(["javascript", "python"] as const).map((lang) => (
                  <button
                    key={lang}
                    type="button"
                    className={cn(
                      "px-2.5 py-1 text-[11px] font-medium transition-colors",
                      editLanguage === lang ? "bg-accent/10 text-accent" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
                    )}
                    onClick={() => setEditLanguage(lang)}
                  >
                    {lang === "javascript" ? "JavaScript" : "Python"}
                  </button>
                ))}
              </div>
            </div>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <label className="content-compact text-default-secondary">
                  {editLanguage === "javascript" ? t_i18n("JavaScript Function") : t_i18n("Python Function")}
                </label>
                <div className="flex items-center gap-2">
                  <VariableInsertButton textareaRef={jsRef} value={editJsFunction} onChange={setEditJsFunction} />
                  <CustomToolCodeGenerator
                    language={editLanguage}
                    code={editJsFunction}
                    onGenerated={(result) => setEditJsFunction(result.code)}
                    toolName={editName}
                    toolDescription={editDescription}
                    parameters={editProperties}
                  />
                </div>
              </div>
              <p className="text-[9px] text-default-secondary">
                {editLanguage === "javascript"
                  ? <>{t_i18n("Parameters:")} <code className="text-accent">$param_name</code>. {t_i18n("Output:")} <code className="text-accent">console.log()</code></>
                  : <>{t_i18n("Parameters:")} <code className="text-accent">param_name</code>. {t_i18n("Output:")} <code className="text-accent">print()</code></>}
              </p>
              <textarea
                ref={jsRef}
                className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                value={editJsFunction}
                onChange={(e) => setEditJsFunction(e.target.value)}
              />
            </div>
          </div>

          {/* Execution timeout */}
          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Execution Timeout (seconds)")}</label>
            <p className="text-[9px] text-default-secondary">{t_i18n("Maximum time the tool is allowed to run before being killed. Default: 300s (5 min).")}</p>
            <Input aria-label={t_i18n("Execution Timeout (seconds)")}
              type="number"
              min={1}
              max={3600}
              value={editTimeout}
              onChange={(e) => setEditTimeout(Math.max(1, Math.min(3600, Number(e.target.value) || 300)))}
              className="h-8 content-base font-mono-plex w-40"
            />
          </div>
          </>)}

          {/* Hide from chat toggle */}
          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Hide from chat")}</p>
              <p className="text-[11px] text-default-secondary">{t_i18n("When enabled, this tool won't appear in the chat tool picker or profile settings. It will still be available for agent configuration.")}</p>
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


export function ConfigTab({ tool, canManage }: { tool: CustomTool; canManage: boolean }) {
  return (
    <div className="space-y-6">
      <ConfigurationCard tool={tool} canManage={canManage} />
    </div>
  );
}
