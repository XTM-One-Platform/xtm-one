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
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useCreateKnowledgeBase } from "@/hooks/api/useKnowledgeBaseApi";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { useFormatter } from "@/i18n";

export default function NewKnowledgeBase() {
  const aiOff = useAiActionDisabled();
  const navigate = useNavigate();
  const create = useCreateKnowledgeBase();
  const { isAdmin } = useAuth();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const agenticAIEnabled = useAgenticAIEnabled();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating knowledge bases" />;

  const handleCreate = () => {
    create.mutate(
      { name, description: description || undefined, tags: newTags.length > 0 ? newTags : undefined, is_company_managed: isCompanyManaged || undefined },
      {
        onSuccess: (newKb) => {
          toast({ title: t_i18n("Knowledge base created"), variant: "success" });
          navigate(`/knowledge/${newKb.id}`);
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      <BackButton to="/knowledge" label="Back to Knowledge Bases" />
      <div className="flex items-center gap-3">
        <Icon name="database" size={24} className="text-accent" />
        <h1 className="text-2xl font-bold">{t_i18n("New Knowledge Base")}</h1>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField id="new-kb-name" label={t_i18n("Name")} required helper={t_i18n("The name people and agents see when they pick this knowledge base.")}>
              <Input id="new-kb-name" aria-label={t_i18n("Name")} aria-describedby={settingsFieldHintId("new-kb-name")} value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("e.g. Product Documentation")} />
            </SettingsField>
            <SettingsField id="new-kb-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-kb-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-kb-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
          </div>
          <SettingsField id="new-kb-description" label={t_i18n("Description (optional)")} helper={t_i18n("What it holds, shown in the list and to agents choosing a source.")}>
            <Input id="new-kb-description" aria-label={t_i18n("Description (optional)")} aria-describedby={settingsFieldHintId("new-kb-description")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What content will this knowledge base contain?")} />
          </SettingsField>
          <div className="rounded-sm bg-accent/5 p-3">
            <p className="content-compact text-accent">{t_i18n("After creation, you'll be able to upload files, paste text, and configure sync sources (GitHub, Notion, OneDrive, Google Drive, websites).")}</p>
          </div>
          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Icon name="building-2" size={16} className="text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed knowledge base")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed knowledge base")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/knowledge")}>{t_i18n("Cancel")}</Button>
            <Button onClick={handleCreate} disabled={!name.trim() || create.isPending || aiOff}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create Knowledge Base")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
