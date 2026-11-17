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

/**
 * Workspace knowledge, platform-wide: how much of a model's context
 * window a workspace's sources may fill before they are searched instead, and
 * how long a filed conversation stays quiet before it is indexed. Admin only,
 * like the route (`/settings/workspace-knowledge`).
 */
import { useState } from "react";
import { FolderKanban } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import {
  useUpdateWorkspaceKnowledgeSettings,
  useWorkspaceKnowledgeSettings,
} from "@/hooks/api/useWorkspaceApi";
import { useFormatter } from "@/i18n";
import type { WorkspaceKnowledgeConfig } from "@/types";

/** The bounds of `WorkspaceKnowledgeConfig` (`api/v1/settings.py`). */
const PERCENT = { min: 5, max: 40 };
const MINUTES = { min: 5, max: 1440 };

const within = (value: number, { min, max }: { min: number; max: number }) =>
  Number.isInteger(value) && value >= min && value <= max;

export function WorkspaceKnowledgeSettingsCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const config = useWorkspaceKnowledgeSettings();
  const save = useUpdateWorkspaceKnowledgeSettings();
  const [draft, setDraft] = useState<WorkspaceKnowledgeConfig | null>(null);
  const value = draft ?? config.data;
  if (!value) return null;

  const valid =
    within(value.sources_context_percent, PERCENT) && within(value.conversation_idle_minutes, MINUTES);
  const set = (field: keyof WorkspaceKnowledgeConfig, raw: string) =>
    setDraft({ ...value, [field]: Number(raw) });

  const submit = async () => {
    try {
      await save.mutateAsync(value);
      setDraft(null);
      toast({ title: t_i18n("Workspace knowledge settings saved"), variant: "success" });
    } catch (err) {
      toast({
        title: t_i18n("The settings could not be saved"),
        description: err instanceof Error && err.message ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderKanban className="h-4 w-4 text-accent" />
          {t_i18n("Workspace knowledge")}
        </CardTitle>
        <CardDescription>
          {t_i18n("What every workspace gives the agents working in its conversations.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="ws-sources-percent">{t_i18n("Sources given whole (% of the context window)")}</Label>
            <Input
              id="ws-sources-percent"
              aria-label={t_i18n("Sources given whole (% of the context window)")}
              type="number"
              min={PERCENT.min}
              max={PERCENT.max}
              value={value.sources_context_percent}
              onChange={(e) => set("sources_context_percent", e.target.value)}
            />
            <p className="content-caption text-default-secondary">
              {t_i18n("Beyond this share, a workspace's sources are searched instead ({min}-{max}).", PERCENT)}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ws-idle-minutes">{t_i18n("Conversation indexed after (minutes of quiet)")}</Label>
            <Input
              id="ws-idle-minutes"
              aria-label={t_i18n("Conversation indexed after (minutes of quiet)")}
              type="number"
              min={MINUTES.min}
              max={MINUTES.max}
              value={value.conversation_idle_minutes}
              onChange={(e) => set("conversation_idle_minutes", e.target.value)}
            />
            <p className="content-caption text-default-secondary">
              {t_i18n("A filed conversation becomes searchable once it has been quiet this long ({min}-{max}).", MINUTES)}
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-2">
          {draft && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
              {t_i18n("Cancel")}
            </Button>
          )}
          <Button size="sm" disabled={!draft || !valid || save.isPending} onClick={submit}>
            {t_i18n("Save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
