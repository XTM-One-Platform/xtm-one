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
import {
  Building2,
  Link as LinkIcon,
  ClipboardPaste,
  Upload,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useCreateSkill, useUploadSkill } from "@/hooks/api/useSkillApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { AgentSkill } from "@/types";
import { NO_SOURCE_CREDENTIAL, PrivateSourceFields } from "./PrivateSourceFields";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { Spinner } from "@filigran/design-system";

type AddMode = "url" | "paste" | "upload";

// ---------------------------------------------------------------------------
// New Skill Form (full page)
// ---------------------------------------------------------------------------

export function NewSkillForm() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const createSkill = useCreateSkill();
  const uploadSkill = useUploadSkill();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  const [addMode, setAddMode] = useState<AddMode>("url");
  const [url, setUrl] = useState("");
  const [sourceIntegrationId, setSourceIntegrationId] = useState(NO_SOURCE_CREDENTIAL);
  const [sourceRef, setSourceRef] = useState("");
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [compatibility, setCompatibility] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);

  const aiOff = useAiActionDisabled();
  /* `isPending` drives the labels ("Creating...", "Uploading...") and the drop
     zone's spinner, so folding `aiOff` in made the form permanently claim a
     save was in flight (/code-review, 2026-08-28). It means "a request is in
     flight" and nothing else; `blocked` is what disables. */
  const isPending = createSkill.isPending || uploadSkill.isPending;
  const blocked = isPending || aiOff;

  // A company-managed skill lends its private source and the variables it
  // references to every user.
  const handleCreateUrl = async () => {
    if (!url.trim()) return;
    const credentialId = sourceIntegrationId !== NO_SOURCE_CREDENTIAL ? sourceIntegrationId : undefined;
    if (isCompanyManaged && !(await confirmSharing({ entity_type: "skill", change: { name: url.trim(), source_integration_id: credentialId ?? null, is_company_managed: true } }, { confirmLabel: t_i18n("Create") }))) return;
    createSkill.mutate(
      {
        url: url.trim(),
        source_integration_id: credentialId,
        // A tracked branch only applies to the authenticated read.
        source_ref: credentialId && sourceRef.trim() ? sourceRef.trim() : undefined,
        tags: tags.length > 0 ? tags : undefined,
        is_company_managed: isCompanyManaged || undefined,
      },
      {
        onSuccess: (data) => {
          const skill = data as AgentSkill;
          toast({ title: t_i18n("Skill created"), description: t_i18n("\"{name}\" has been fetched and cached.", { name: skill.name }), variant: "success" });
          navigate(`/skills/${skill.id}`);
        },
        onError: (err) => {
          toast({ title: t_i18n("Failed to create skill"), description: err.message, variant: "error" });
        },
      },
    );
  };

  const handleCreateContent = async () => {
    if (!content.trim()) return;
    if (isCompanyManaged && !(await confirmSharing({ entity_type: "skill", change: { name: name.trim(), content, is_company_managed: true } }, { confirmLabel: t_i18n("Create") }))) return;
    createSkill.mutate(
      {
        content: content.trim(),
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        compatibility: compatibility.trim() || undefined,
        tags: tags.length > 0 ? tags : undefined,
        is_company_managed: isCompanyManaged || undefined,
      },
      {
        onSuccess: (data) => {
          const skill = data as AgentSkill;
          toast({ title: t_i18n("Skill created"), description: t_i18n("\"{name}\" has been saved.", { name: skill.name }), variant: "success" });
          navigate(`/skills/${skill.id}`);
        },
        onError: (err) => {
          toast({ title: t_i18n("Failed to create skill"), description: err.message, variant: "error" });
        },
      },
    );
  };

  const handleUpload = (file: File) => {
    uploadSkill.mutate(file, {
      onSuccess: (data) => {
        const skill = data as AgentSkill;
        toast({ title: t_i18n("Skill created"), description: t_i18n("\"{name}\" has been uploaded.", { name: skill.name }), variant: "success" });
        navigate(`/skills/${skill.id}`);
      },
      onError: (err) => {
        toast({ title: t_i18n("Failed to upload skill"), description: err.message, variant: "error" });
      },
    });
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleUpload(file);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const modeTabs: { mode: AddMode; icon: typeof LinkIcon; label: string }[] = [
    { mode: "url", icon: LinkIcon, label: "From URL" },
    { mode: "paste", icon: ClipboardPaste, label: "Paste Content" },
    { mode: "upload", icon: Upload, label: "Upload File" },
  ];

  return (
    <div className="space-y-6">
      {consentElement}
      <BackButton to="/skills" label="Back to Skills" />

      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Create Skill")}</h1>
        <p className="mt-1 content-base text-default-secondary">
          {t_i18n("Add a skill from a URL, paste content directly, or upload a file. Skills following the")}{" "}
          <a href="https://agentskills.io" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{t_i18n("Agent Skills spec")}</a>
          {" "}{t_i18n("are auto-parsed for name, description, and compatibility from YAML frontmatter.")}
        </p>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          {/* Mode tabs */}
          <div>
            <Label className="mb-2 block text-default-primary">{t_i18n("Source Type")}</Label>
            <div className="flex gap-1">
              {modeTabs.map(({ mode, icon: Icon, label }) => (
                <button
                  key={mode}
                  onClick={() => setAddMode(mode)}
                  className={cn(
                    "flex items-center gap-1.5 px-4 py-2 rounded-sm content-base font-medium transition-colors",
                    addMode === mode
                      ? "bg-accent/15 text-accent"
                      : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
                  )}
                >
                  <Icon className="h-4 w-4" />
                  {t_i18n(label)}
                </button>
              ))}
            </div>
          </div>

          {/* URL mode */}
          {addMode === "url" && (
            <div className="space-y-4">
              <SettingsField
                id="new-skill-url"
                label={t_i18n("Skill URL")}
                required
                helper={t_i18n("Paste a URL to a SKILL.md file. Name, description, and compatibility are auto-detected from YAML frontmatter.")}
              >
                <Input id="new-skill-url" aria-label={t_i18n("Skill URL")} aria-describedby={settingsFieldHintId("new-skill-url")}
                  placeholder="https://raw.githubusercontent.com/org/repo/main/skills/my-skill/SKILL.md"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  className="font-mono-plex"
                  onKeyDown={(e) => { if (e.key === "Enter") handleCreateUrl(); }}
                  autoFocus
                />
              </SettingsField>

              <PrivateSourceFields
                integrationId={sourceIntegrationId}
                onIntegrationIdChange={setSourceIntegrationId}
                sourceRef={sourceRef}
                onSourceRefChange={setSourceRef}
              />

              <SettingsField id="new-skill-url-tags" label={t_i18n("Tags (optional)")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
                <TagInput id="new-skill-url-tags" aria-label={t_i18n("Tags (optional)")} aria-describedby={settingsFieldHintId("new-skill-url-tags")} value={tags} onChange={setTags} placeholder={t_i18n("Add tags…")} />
              </SettingsField>
            </div>
          )}

          {/* Paste mode */}
          {addMode === "paste" && (
            <div className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-3">
                <SettingsField id="new-skill-name" label={t_i18n("Name (optional)")} helper={t_i18n("Empty: read from the frontmatter of the content.")}>
                  <Input id="new-skill-name" aria-label={t_i18n("Name (optional)")} aria-describedby={settingsFieldHintId("new-skill-name")} placeholder={t_i18n("Auto-detected from frontmatter")} value={name} onChange={(e) => setName(e.target.value)} />
                </SettingsField>
                <SettingsField
                  id="new-skill-compatibility"
                  label={t_i18n("Compatibility (optional)")}
                  helper={t_i18n("The products or tools this skill is written for.")}
                  info={t_i18n("Free text shown to agents and people choosing a skill, for example xtm-one. Empty: read from the frontmatter.")}
                >
                  <Input id="new-skill-compatibility" aria-label={t_i18n("Compatibility (optional)")} aria-describedby={settingsFieldHintId("new-skill-compatibility")} placeholder={t_i18n("e.g. xtm-one")} value={compatibility} onChange={(e) => setCompatibility(e.target.value)} />
                </SettingsField>
                <SettingsField id="new-skill-paste-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
                  <TagInput id="new-skill-paste-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-skill-paste-tags")} value={tags} onChange={setTags} placeholder={t_i18n("Add tags…")} />
                </SettingsField>
              </div>

              <SettingsField id="new-skill-description" label={t_i18n("Description (optional)")} helper={t_i18n("What the skill teaches, shown in lists. Empty: read from the frontmatter.")}>
                <textarea
                  id="new-skill-description"
                  aria-label={t_i18n("Description (optional)")}
                  aria-describedby={settingsFieldHintId("new-skill-description")}
                  className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                  placeholder={t_i18n("Describe what this skill teaches the agent.")}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </SettingsField>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label htmlFor="new-skill-content" className="flex items-center gap-1 text-default-primary">
                    {t_i18n("Skill Content (markdown)")}
                    <span className="text-feedback-error-primary" aria-label={t_i18n("Required")}>*</span>
                  </Label>
                  <VariableInsertButton textareaRef={contentRef} value={content} onChange={setContent} />
                </div>
                <p id="new-skill-content-hint" className="content-compact text-default-secondary">
                  {t_i18n("Paste skill instructions. YAML frontmatter (if present) is parsed for name, description, and compatibility.")}
                </p>
                <textarea
                  id="new-skill-content"
                  aria-describedby="new-skill-content-hint"
                  ref={contentRef}
                  className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                  placeholder={t_i18n("Paste your skill instructions here (markdown)...")}
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  autoFocus
                />
              </div>
            </div>
          )}

          {/* Upload mode */}
          {addMode === "upload" && (
            <div className="space-y-4">
              <div className="space-y-1.5">
                <Label className="block text-default-primary">{t_i18n("Upload File")}</Label>
                <p className="content-compact text-default-secondary">
                  {t_i18n("Upload a")} <code className="text-accent">.md</code> {t_i18n("or")} <code className="text-accent">.txt</code> {t_i18n("file. YAML frontmatter (if present) is parsed for metadata.")}
                </p>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept=".md,.txt"
                onChange={handleFileChange}
                className="hidden"
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={blocked}
                className={cn(
                  "w-full flex flex-col items-center gap-2 rounded-sm border-2 border-dashed border-elevation-subtle-soft py-12",
                  "text-default-secondary hover:border-accent/30 hover:text-accent transition-colors cursor-pointer",
                  blocked && "opacity-50 pointer-events-none",
                )}
              >
                {isPending && <Spinner size="xl" />}{!isPending && <Upload className="h-8 w-8" />}
                <span className="content-base">
                  {isPending ? t_i18n("Uploading...") : t_i18n("Click to select a file")}
                </span>
                <span className="content-compact text-default-secondary">
                  {t_i18n(".md or .txt files only")}
                </span>
              </button>

              <SettingsField id="new-skill-upload-tags" label={t_i18n("Tags (optional)")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
                <TagInput id="new-skill-upload-tags" aria-label={t_i18n("Tags (optional)")} aria-describedby={settingsFieldHintId("new-skill-upload-tags")} value={tags} onChange={setTags} placeholder={t_i18n("Add tags…")} />
              </SettingsField>
            </div>
          )}

          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed skill")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed skill")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/skills")}>{t_i18n("Cancel")}</Button>
            {addMode === "url" && (
              <Button onClick={handleCreateUrl} disabled={!url.trim() || blocked}>
                {isPending ? t_i18n("Creating...") : t_i18n("Fetch & Create")}
              </Button>
            )}
            {addMode === "paste" && (
              <Button onClick={handleCreateContent} disabled={!content.trim() || blocked}>
                {isPending ? t_i18n("Creating...") : t_i18n("Create Skill")}
              </Button>
            )}
            {addMode === "upload" && (
              <Button onClick={() => fileInputRef.current?.click()} disabled={blocked}>
                {isPending ? t_i18n("Uploading...") : t_i18n("Select & Upload")}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
