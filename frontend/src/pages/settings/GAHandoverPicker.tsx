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
 * Settings > AI and models > General Assistant > Agent Handover.
 *
 * The General Assistant serves every user, and `general_chat` only offers the
 * targets the CHATTING user can see: like every other resource bound here, a
 * handover target is a company-managed agent every user reaches (enabled, not
 * narrowed with `access_restricted`), and the settings API refuses to add any
 * other. Laid out for a large fleet: the selected agents first, each
 * with its handover type, then a searchable, height-capped list of the agents
 * that can be added. A target saved before that rule stays listed with the
 * reason it is not reached, so it can be removed. While no target has a type,
 * each one runs every type and is shown so (`lib/handover-types.ts`).
 */
import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeftRight, Bot, Plus, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { GA_HANDOVER_TYPES, HANDOVER_TYPE_CONFIG, ICON_MAP, type GAHandoverType } from "@/lib/constants";
import { ALL_HANDOVER_TYPES, isLegacyHandoverMap, shownHandoverType } from "@/lib/handover-types";
import { cn } from "@/lib/utils";
import type { Agent } from "@/types";

interface GAHandoverPickerProps {
  /** Every agent the admin can see. */
  agents: Agent[];
  selectedIds: string[];
  types: Record<string, string>;
  onToggle: (agentId: string) => void;
  onTypeChange: (agentId: string, type: GAHandoverType) => void;
}

function byName(a: Agent, b: Agent): number {
  return a.name.localeCompare(b.name);
}

function isReachable(agent: Agent | undefined): agent is Agent {
  return !!agent && agent.enabled && agent.is_company_managed && !agent.access_restricted;
}

export function GAHandoverPicker({ agents, selectedIds, types, onToggle, onTypeChange }: GAHandoverPickerProps) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");

  const agentsById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);
  const eligible = useMemo(() => agents.filter(isReachable).sort(byName), [agents]);
  const selected = useMemo(
    () =>
      selectedIds
        .map((id) => ({ id, agent: agentsById.get(id) }))
        .sort(
          (a, b) =>
            Number(isReachable(b.agent)) - Number(isReachable(a.agent)) ||
            (a.agent?.name ?? a.id).localeCompare(b.agent?.name ?? b.id),
        ),
    [selectedIds, agentsById],
  );

  const legacy = isLegacyHandoverMap(types);
  const selectedSet = new Set(selectedIds);
  const unselected = eligible.filter((agent) => !selectedSet.has(agent.id));
  const query = search.trim().toLowerCase();
  const addable = query
    ? unselected.filter((agent) =>
        [agent.name, agent.slug ?? "", agent.description ?? ""].some((field) => field.toLowerCase().includes(query)),
      )
    : unselected;

  const unreachableReason = (agent: Agent | undefined): string | null => {
    if (!agent) return t_i18n("Deleted, or not visible to you");
    if (!agent.is_company_managed) return t_i18n("Not company-managed: only the users who can see it reach it");
    if (agent.access_restricted) return t_i18n("Restricted access: only the users granted access reach it");
    if (!agent.enabled) return t_i18n("Disabled: the General Assistant skips it");
    return null;
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <ArrowLeftRight className="h-4 w-4 text-accent" />
        <span className="content-base font-medium">{t_i18n("Agent Handover")}</span>
        {selectedIds.length > 0 && (
          <Badge variant="secondary" className="content-caption h-4 px-1.5">
            {t_i18n("{count} selected", { count: selectedIds.length })}
          </Badge>
        )}
      </div>
      <p className="content-compact text-default-secondary">
        {t_i18n("Choose the agents the General Assistant can hand conversations over to, and how. Only company-managed agents are offered: they are the ones every user can reach.")}
      </p>

      {selected.length > 0 && (
        <div className="space-y-1.5" data-testid="ga-handover-selected">
          {selected.map(({ id, agent }) => {
            const reason = unreachableReason(agent);
            const Icon = (agent && ICON_MAP[agent.icon]) || Bot;
            const type = shownHandoverType(types, id, GA_HANDOVER_TYPES);
            const name = agent?.name ?? t_i18n("Unknown agent");
            return (
              <div
                key={id}
                className={cn(
                  "flex items-center gap-3 rounded-sm border px-3 py-2",
                  reason ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/5" : "border-accent/30 bg-accent/5",
                )}
              >
                <div
                  className={cn(
                    "flex h-7 w-7 shrink-0 items-center justify-center rounded-sm",
                    reason ? "bg-feedback-alert-primary/10" : "bg-accent/15",
                  )}
                >
                  {reason ? (
                    <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary" />
                  ) : (
                    <Icon className="h-3.5 w-3.5 text-accent" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p className={cn("content-base font-medium truncate", reason && "text-default-secondary")}>{name}</p>
                    {agent?.slug && (
                      <span className="content-caption font-mono-plex text-default-secondary shrink-0">@{agent.slug}</span>
                    )}
                  </div>
                  {reason && <p className="content-caption text-feedback-warning-primary">{reason}</p>}
                </div>
                {!reason && (
                  <Select
                    value={type}
                    onValueChange={(value) => {
                      if (value !== ALL_HANDOVER_TYPES) onTypeChange(id, value as GAHandoverType);
                    }}
                  >
                    <SelectTrigger className="w-40 shrink-0" aria-label={t_i18n("Handover type")}>
                      <SelectValue>
                        {type === ALL_HANDOVER_TYPES ? (
                          <span className="text-default-primary">{t_i18n("All types")}</span>
                        ) : (
                          <span className={HANDOVER_TYPE_CONFIG[type].color}>{t_i18n(HANDOVER_TYPE_CONFIG[type].label)}</span>
                        )}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent className="max-w-xs">
                      {legacy && (
                        <SelectItem value={ALL_HANDOVER_TYPES}>
                          <span className="flex flex-col gap-0.5">
                            <span className="content-compact font-medium text-default-primary">{t_i18n("All types")}</span>
                            <span className="content-caption text-default-secondary whitespace-normal">
                              {t_i18n("Legacy mode: no agent has a type yet, so the General Assistant can delegate to, consult or transfer to each one. Picking a type for one agent makes the others Delegate.")}
                            </span>
                          </span>
                        </SelectItem>
                      )}
                      {GA_HANDOVER_TYPES.map((option) => (
                        <SelectItem key={option} value={option}>
                          <span className="flex flex-col gap-0.5">
                            <span className={cn("content-compact font-medium", HANDOVER_TYPE_CONFIG[option].color)}>
                              {t_i18n(HANDOVER_TYPE_CONFIG[option].label)}
                            </span>
                            <span className="content-caption text-default-secondary whitespace-normal">
                              {t_i18n(HANDOVER_TYPE_CONFIG[option].description)}
                            </span>
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
                <Tip content={t_i18n("Remove")}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 shrink-0 text-default-secondary hover:text-default-primary"
                    onClick={() => onToggle(id)}
                    aria-label={t_i18n("Remove {name}", { name })}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </Tip>
              </div>
            );
          })}
        </div>
      )}

      {eligible.length === 0 ? (
        <div className="rounded-sm border border-dashed border-elevation-subtle-soft py-4 text-center">
          <ArrowLeftRight className="h-5 w-5 text-default-disabled mx-auto mb-1" />
          <p className="content-compact text-default-secondary">{t_i18n("No company-managed agents yet")}</p>
          <p className="content-caption text-default-secondary mt-0.5">
            {t_i18n("Make an agent company-managed from its configuration to let the General Assistant hand over to it.")}
          </p>
        </div>
      ) : unselected.length > 0 ? (
        <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover">
          <div className="border-b border-elevation-subtle-soft p-2">
            <SearchInput
              placeholder={t_i18n("Search company-managed agents...")}
              value={search}
              onChange={setSearch}
              className="max-w-full"
            />
          </div>
          <div className="max-h-64 overflow-y-auto p-1" data-testid="ga-handover-candidates">
            {addable.length > 0 ? (
              addable.map((agent) => {
                const Icon = ICON_MAP[agent.icon] ?? Bot;
                return (
                  <button
                    key={agent.id}
                    type="button"
                    className="group flex w-full items-center gap-3 rounded-sm px-2.5 py-2 text-left transition-colors hover:bg-elevation-hover"
                    onClick={() => onToggle(agent.id)}
                  >
                    <Icon className="h-4 w-4 shrink-0 text-default-secondary group-hover:text-accent" />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span className="content-base truncate">{agent.name}</span>
                        {agent.slug && (
                          <span className="content-caption font-mono-plex text-default-secondary shrink-0">@{agent.slug}</span>
                        )}
                      </span>
                      {agent.description && (
                        <span className="block content-caption text-default-secondary truncate">{agent.description}</span>
                      )}
                    </span>
                    <Plus className="h-4 w-4 shrink-0 text-default-disabled group-hover:text-accent" />
                  </button>
                );
              })
            ) : (
              <p className="px-2.5 py-3 text-center content-compact text-default-secondary">
                {t_i18n("No company-managed agent matches your search.")}
              </p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
