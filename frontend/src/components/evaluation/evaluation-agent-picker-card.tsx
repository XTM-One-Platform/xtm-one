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
import { Bot, X, Star } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InfoLabel } from "@/components/ui/info-tip";
import { Tip } from "@/components/ui/tooltip";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useDebounce } from "@/hooks/useDebounce";
import { useEvaluationAgents } from "@/hooks/api/useEvaluationApi";
import { ICON_MAP } from "@/lib/constants";
import { useFormatter } from "@/i18n";

interface EvaluationAgentPickerCardAgent {
  id: string;
  name: string;
  slug: string | null;
  icon?: string | null;
  is_baseline?: boolean;
}

interface EvaluationAgentPickerCardSearchResult {
  id: string;
  name: string;
  slug: string | null;
  icon: string;
}

export interface EvaluationAgentPickerCardProps {
  agents: EvaluationAgentPickerCardAgent[];
  boundIds: string[];
  canManage: boolean;
  editing: boolean;
  /**
   * Receives the full search result, not just its id: the creation page has no
   * server round-trip after adding, so it caches `name`/`slug`/`icon` from it.
   * `ConfigTab` reads only `.id`.
   */
  onAdd: (agent: EvaluationAgentPickerCardSearchResult) => void;
  onRemove: (agentId: string) => void;
}

/**
 * The "Agents" card of the evaluation creation page and of `EvaluationDetail.tsx`'s
 * Configuration tab: a search-and-add picker plus the selected agents. The
 * creation page always passes `editing`; the Configuration tab also renders it read-only.
 */
export function EvaluationAgentPickerCard({ agents, boundIds, canManage, editing, onAdd, onRemove }: EvaluationAgentPickerCardProps) {
  const { t_i18n } = useFormatter();
  const [agentSearch, setAgentSearch] = useState("");
  const debouncedSearch = useDebounce(agentSearch, 300);
  const { data: searchResults, isPending, isError } = useEvaluationAgents(debouncedSearch, boundIds);
  // Until the server answers what is typed (or answers at all: on the first
  // open, after each addition), the rows would be the previous query's or none:
  // none is offered, so Enter cannot add an agent the field does not match.
  const searching = agentSearch !== debouncedSearch || isPending;
  // A failed search is not an empty answer: no row, and the list says so.
  const found = searching || isError ? [] : (searchResults ?? []);

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Bot className="h-3.5 w-3.5 text-default-secondary" />
          <CardTitle className="text-base font-medium">{t_i18n("Agents")}</CardTitle>
          <Badge variant="outline" className="content-caption">{t_i18n("{count} selected", { count: agents.length })}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Search picker: the list opens above the page, never under the next card. */}
        {canManage && editing && (
          <SearchableSelect
            // Keyed on the count: each addition starts an empty field again.
            key={agents.length}
            aria-label={t_i18n("Search agents to add...")}
            placeholder={t_i18n("Search agents to add...")}
            options={found.map((agent) => agentSelectOption(agent, { description: agent.slug ?? undefined }))}
            value=""
            onValueChange={(id) => {
              const agent = found.find((a) => a.id === id);
              if (!agent) return;
              onAdd(agent);
              // The field starts empty again (the key), and so does the search.
              setAgentSearch("");
            }}
            onSearchChange={setAgentSearch}
            // The server's answer, in its order.
            sort={false}
            emptyMessage={
              searching
                ? t_i18n("Searching…")
                : isError
                  ? t_i18n("The agents could not be loaded.")
                  : agentSearch
                    ? t_i18n("No matching agents.")
                    : t_i18n("All agents are already added.")
            }
          />
        )}

        {/* Selected agents list */}
        {agents.length === 0 ? (
          <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
            <Bot className="h-6 w-6 text-default-secondary mx-auto mb-2" />
            <p className="content-compact text-default-secondary">{t_i18n("No agents added yet. Search and add agents above.")}</p>
          </div>
        ) : (
          <div className="space-y-1">
            {agents.map((agent) => (
              <AgentRow
                key={agent.id}
                agent={agent}
                canManage={canManage}
                editing={editing}
                onRemove={onRemove}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Display-only agent row of the Agents card; the baseline is shown read-only.
 * Baseline toggling and agent tags live in `AgentTagsBaselinePanel`
 * (`EvaluationDetail.tsx`), which renders its own `AgentTagRow`.
 */
function AgentRow({ agent, canManage, editing, onRemove }: {
  agent: EvaluationAgentPickerCardAgent;
  canManage: boolean;
  editing: boolean;
  onRemove: (agentId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const IconComp = ICON_MAP[agent.icon as keyof typeof ICON_MAP] ?? Bot;
  const isBaseline = !!agent.is_baseline;

  return (
    <div className="rounded-sm bg-elevation-default px-4 py-2.5 group hover:bg-elevation-hover transition-colors">
      <div className="flex items-center gap-3">
        <IconComp className="h-4 w-4 text-accent shrink-0" />
        <div className="min-w-0 flex-1 flex items-center gap-2">
          <p className="content-base font-medium truncate">{agent.name}</p>
          {isBaseline && (
            <InfoLabel content={t_i18n(EVALUATION_TERMS.baselineAgent)} className="shrink-0">
              <Badge variant="outline" className="gap-1 content-caption border-feedback-alert-primary/30 text-feedback-warning-primary">
                <Star className="h-2.5 w-2.5 fill-feedback-alert-primary text-feedback-warning-primary" />
                {t_i18n("Baseline")}
              </Badge>
            </InfoLabel>
          )}
        </div>
        {agent.slug && <code className="content-caption text-default-secondary shrink-0">{agent.slug}</code>}
        {canManage && editing && (
          <Tip content={t_i18n("Remove agent")}>
            <Button variant="ghost" size="sm" className="h-7 w-7 p-0 text-default-secondary hover:text-feedback-error-primary transition-all" aria-label={t_i18n("Remove agent") + ` ${agent.name}`} onClick={() => onRemove(agent.id)}>
              <X className="h-3.5 w-3.5" />
            </Button>
          </Tip>
        )}
      </div>
    </div>
  );
}
