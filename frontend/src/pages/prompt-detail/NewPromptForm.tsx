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
import { Icon } from "@filigran/design-system";
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
import { useCreatePrompt } from "@/hooks/api/usePromptApi";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { Prompt } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

// ---------------------------------------------------------------------------
// New Prompt Form
// ---------------------------------------------------------------------------

export function NewPromptForm() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const create = useCreatePrompt();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [newTags, setNewTags] = useState<string[]>([]);
  const contentRef = useRef<HTMLTextAreaElement>(null);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);

  const handleCreate = async () => {
    if (!name.trim()) return;
    // A company-managed prompt lends the variables it references to every user.
    if (isCompanyManaged && !(await confirmSharing({ entity_type: "prompt", change: { name, content, is_company_managed: true } }, { confirmLabel: t_i18n("Create") }))) return;
    create.mutate(
      {
        name,
        description,
        content,
        tags: newTags.length > 0 ? newTags : undefined,
        is_company_managed: isCompanyManaged,
      },
      {
        onSuccess: (data) => {
          const prompt = data as Prompt;
          toast({ title: t_i18n("Prompt created"), description: t_i18n("{name} has been created.", { name: prompt.name }), variant: "success" });
          navigate(`/prompts/${prompt.id}`);
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      {consentElement}
      <BackButton to="/prompts" label="Back to Prompts" />

      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Create Prompt")}</h1>
        <p className="mt-1 content-base text-default-secondary">
          {t_i18n("Define a reusable prompt template. Prompts can be attached to agents or used directly in chat.")}
        </p>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          {/* Name / Tags */}
          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField id="new-prompt-name" label={t_i18n("Prompt Name")} required helper={t_i18n("Shown in the prompt list and when attaching it to an agent.")}>
              <Input id="new-prompt-name" aria-label={t_i18n("Prompt Name")} aria-describedby={settingsFieldHintId("new-prompt-name")} placeholder={t_i18n("e.g. Code Review Guidelines")} value={name} onChange={(e) => setName(e.target.value)} />
            </SettingsField>
            <SettingsField id="new-prompt-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-prompt-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-prompt-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
          </div>

          <SettingsField id="new-prompt-description" label={t_i18n("Description")} helper={t_i18n("When and how to use this prompt, shown in the list.")}>
            <textarea
              id="new-prompt-description"
              aria-label={t_i18n("Description")}
              aria-describedby={settingsFieldHintId("new-prompt-description")}
              className={`flex min-h-[80px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              placeholder={t_i18n("Describe when and how this prompt should be used.")}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </SettingsField>

          {/* Prompt Content */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label htmlFor="new-prompt-content" className="block text-default-primary">{t_i18n("Prompt Content")}</Label>
              <VariableInsertButton textareaRef={contentRef} value={content} onChange={setContent} />
            </div>
            <p id="new-prompt-content-hint" className="content-compact text-default-secondary">
              {t_i18n("The text of the prompt. This will be injected into the agent's system prompt or used directly in chat.")}
            </p>
            <textarea
              ref={contentRef}
              id="new-prompt-content"
              aria-describedby="new-prompt-content-hint"
              className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              placeholder={t_i18n("You are an expert code reviewer. When reviewing code, focus on...")}
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
          </div>

          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Icon name="building-2" size={16} className="text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed prompt")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed prompt")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/prompts")}>{t_i18n("Cancel")}</Button>
            <AiDisabledTip>
            <Button onClick={handleCreate} disabled={!name.trim() || create.isPending || aiOff}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create Prompt")}
            </Button>
            </AiDisabledTip>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
