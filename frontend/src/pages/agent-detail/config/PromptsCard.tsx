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

import { useMemo, useState } from "react";
import {
  FileText, Building2, CheckCircle2, Plus,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CountBadge } from "@/components/ui/count-badge";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { SearchInput } from "@/components/ui/search-input";
import { usePrompts } from "@/hooks/api/usePromptApi";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn, toggleArrayItem } from "@/lib/utils";
import { CONTENT_PREVIEW_LIMIT } from "@/lib/constants";
import type { Agent } from "@/types";

export function PromptsCard({
  agent,
  promptIds,
  setPromptIds,
  editing,
  canManage,
  onStartEdit,
  onSave,
  onCancel,
  saving,
  companyManaged,
}: {
  agent: Agent;
  promptIds: string[];
  setPromptIds: React.Dispatch<React.SetStateAction<string[]>>;
  editing: boolean;
  canManage: boolean;
  onStartEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  companyManaged: boolean;
}) {
  const { t_i18n } = useFormatter();
  // The viewer's prompts plus the ones the agent is bound to (an admin on a
  // colleague's agent sees and keeps them).
  const { data: visiblePrompts } = usePrompts();
  const { data: everyPrompt } = usePrompts({ scope: "all" });
  const allPrompts = useMemo(
    () => withBoundEntities(visiblePrompts, everyPrompt, agent.prompt_ids),
    [visiblePrompts, everyPrompt, agent.prompt_ids],
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [promptSearch, setPromptSearch] = useState("");

  // Prompts available for this agent (respects company-managed constraint, filtered + sorted)
  const allAvailablePrompts = allPrompts?.filter((p: { is_company_managed: boolean }) => !companyManaged || p.is_company_managed) ?? [];
  const availablePrompts = allAvailablePrompts
    .filter((p: { name: string; description?: string }) => {
      if (promptSearch) {
        const q = promptSearch.toLowerCase();
        return p.name.toLowerCase().includes(q) || (p.description ?? "").toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name));

  const togglePrompt = (id: string) => setPromptIds((prev) => toggleArrayItem(prev, id));

  // Resolve currently attached prompts for read-only view (sorted alphabetically)
  const attachedPrompts = (agent.prompt_ids || [])
    .map((id) => allPrompts?.find((p: { id: string }) => p.id === id))
    .filter(Boolean)
    .sort((a, b) => ((a as { name: string }).name).localeCompare((b as { name: string }).name));

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <div>
          <CardTitle className="content-base flex items-center gap-2">
            <FileText className="h-4 w-4 text-accent" /> {t_i18n("Prompts")}
            {(agent.prompt_ids || []).length > 0 && !editing && (
              <CountBadge variant="secondary">{(agent.prompt_ids || []).length}</CountBadge>
            )}
          </CardTitle>
          <p className="content-caption text-default-secondary mt-0.5">
            {t_i18n("Reusable prompt templates injected into the agent's system prompt at runtime.")}
          </p>
          {companyManaged && <p className="content-caption text-feedback-warning-primary mt-0.5 flex items-center gap-1"><Building2 className="h-3 w-3" /> {t_i18n("Only company-managed prompts are shown.")}</p>}
        </div>
        <CardEditActions editing={editing} canEdit={canManage} onEdit={onStartEdit} onCancel={() => { setPromptSearch(""); onCancel(); }} onSave={() => { setPromptSearch(""); onSave(); }} saveDisabled={saving} />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0 space-y-3">
        {editing ? (
          <>
            <p className="content-caption text-default-secondary mb-2">{t_i18n("Click to attach or detach prompts.")}</p>
            {allAvailablePrompts.length > 3 && (
              <SearchInput placeholder={t_i18n("Filter prompts...")} value={promptSearch} onChange={setPromptSearch} className="mb-3 max-w-full" />
            )}

            {/* Prompt picker grid */}
            {availablePrompts.length > 0 ? (
              <div className="grid gap-2 sm:grid-cols-3">
                {availablePrompts.map((prompt: { id: string; name: string; description?: string; is_company_managed?: boolean }) => {
                  const isAttached = promptIds.includes(prompt.id);
                  return (
                    <button
                      key={prompt.id}
                      className={cn(
                        "flex items-center gap-3 rounded-sm border p-3 text-left transition-all",
                        isAttached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                      )}
                      onClick={() => togglePrompt(prompt.id)}
                    >
                      <FileText className={cn("h-5 w-5 shrink-0", isAttached ? "text-accent" : "text-default-secondary")} />
                      <div className="min-w-0 flex-1">
                        <p className={cn("content-base font-medium truncate", isAttached && "text-accent")}>{prompt.name}</p>
                        {prompt.description && <p className="content-caption text-default-secondary truncate">{prompt.description}</p>}
                      </div>
                      {isAttached ? (
                        <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                      ) : (
                        <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                      )}
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="py-4 text-center">
                <FileText className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No prompts available")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create prompts in the Prompts page first.")}</p>
              </div>
            )}
          </>
        ) : (
          <div>
            {attachedPrompts.length > 0 ? (
              <div className="space-y-1.5">
                {attachedPrompts.map((prompt) => {
                  if (!prompt) return null;
                  const p = prompt as { id: string; name: string; description?: string; content?: string; is_company_managed?: boolean };
                  const isExpanded = expandedId === p.id;
                  return (
                    <div key={p.id} className="rounded-sm bg-elevation-hover overflow-hidden">
                      <div
                        className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-elevation-hover transition-colors"
                        onClick={() => setExpandedId(isExpanded ? null : p.id)}
                      >
                        <FileText className="h-4 w-4 text-accent shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium truncate">{p.name}</p>
                          {p.description && <p className="content-caption text-default-secondary truncate">{p.description}</p>}
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          {p.is_company_managed && <Building2 className="h-3 w-3 text-default-disabled" />}
                        </div>
                      </div>
                      {isExpanded && p.content && (
                        <div className="border-t border-elevation-subtle-soft px-3 py-2.5">
                          <div className="rounded-sm bg-elevation-hover p-2 max-h-48 overflow-auto">
                            <pre className="content-caption text-default-secondary whitespace-pre-wrap font-mono-plex leading-relaxed">{p.content.length > CONTENT_PREVIEW_LIMIT ? p.content.slice(0, CONTENT_PREVIEW_LIMIT) + "\n\n" + t_i18n("... (truncated)") : p.content}</pre>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No prompts attached")}</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
