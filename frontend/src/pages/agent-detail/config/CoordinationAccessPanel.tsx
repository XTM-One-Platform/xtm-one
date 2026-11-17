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

import { useMemo } from "react";
import { Bot, CheckCircle2, Lock, Plus, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { readScopeAllowed, SCOPES, withWriteScope, type Access, type Scope } from "@/lib/coordination-access";
import { cn } from "@/lib/utils";
import type { Agent, CoordinationAccess } from "@/types";

/** The read options, read by `t_i18n(READ_LABELS[scope])`. @i18n-keys */
const READ_LABELS = {
  all: "Everyone (default)",
  agents: "Selected agents",
  self: "Only this agent",
} as const;

/** The write options, read by `t_i18n(WRITE_LABELS[scope])`. @i18n-keys */
const WRITE_LABELS = {
  all: "Any company-managed agent (default)",
  agents: "Selected agents",
  self: "Only this agent",
} as const;

type Props = {
  agent: Agent;
  allAgents?: Agent[];
  draft: Access;
  onChange: (next: Access) => void;
};

/**
 * Coordination access: which agents read and write the company-managed
 * work items this agent creates and the shared state it owns. Edited with the
 * built-in tools, like the work-item peers; the default leaves the fleet as it
 * is, and a narrowing only ever takes agents out.
 */
export function CoordinationAccessPanel({ agent, allAgents, draft, onChange }: Props) {
  const { t_i18n } = useFormatter();
  // Only company-managed agents count once listed, so only they are offered.
  const candidates = useMemo(
    () => (allAgents ?? []).filter((a) => a.id !== agent.id && a.is_company_managed),
    [allAgents, agent.id],
  );
  const fleetIds = useMemo(() => {
    const known = new Set(candidates.map((a) => a.id));
    return [...(agent.handover_agent_ids ?? []), ...(agent.work_item_owner_agent_ids ?? [])].filter(
      (id, index, ids) => known.has(id) && ids.indexOf(id) === index,
    );
  }, [agent.handover_agent_ids, agent.work_item_owner_agent_ids, candidates]);

  const toggle = (field: "read_agent_ids" | "write_agent_ids", id: string) => {
    const ids = draft[field];
    onChange({ ...draft, [field]: ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id] });
  };
  const addFleet = (field: "read_agent_ids" | "write_agent_ids") => {
    onChange({ ...draft, [field]: [...draft[field], ...fleetIds.filter((id) => !draft[field].includes(id))] });
  };

  const picker = (field: "read_agent_ids" | "write_agent_ids", title: string) => {
    // Every writer can read: shown selected and fixed in the readers.
    const writers = field === "read_agent_ids" && draft.write === "agents" ? draft.write_agent_ids : [];
    return (
      <div data-testid={`coordination-${field}`} className="mt-2.5">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <p className="text-[11px] font-medium">{title}</p>
          {fleetIds.length > 0 && (
            <Button type="button" variant="ghost" size="sm" className="h-6 gap-1 px-2 content-caption" onClick={() => addFleet(field)}>
              <Plus className="h-3 w-3" /> {t_i18n("Add handover targets and work item peers")}
            </Button>
          )}
        </div>
        {candidates.length === 0 ? (
          <p className="content-caption italic text-default-secondary">{t_i18n("No other company-managed agents.")}</p>
        ) : (
          <div className="grid gap-1.5 sm:grid-cols-2">
            {candidates.map((a) => {
              const locked = writers.includes(a.id);
              const selected = locked || draft[field].includes(a.id);
              const chip = (
                <button
                  key={a.id}
                  type="button"
                  disabled={locked}
                  aria-pressed={selected}
                  onClick={() => toggle(field, a.id)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-sm border px-2.5 py-2 text-left transition-colors",
                    selected ? "border-filigran-ia-primary/40 bg-filigran-ia-primary/10" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                    locked && "cursor-default",
                  )}
                >
                  <Bot className={cn("h-4 w-4 shrink-0", selected ? "text-filigran-ia-primary light:text-filigran-ia-primary" : "text-default-secondary")} />
                  <span className={cn("flex-1 truncate content-compact", selected && "text-filigran-ia-primary light:text-filigran-ia-primary")}>{a.name}</span>
                  {locked ? (
                    <Lock className="h-3.5 w-3.5 shrink-0 text-filigran-ia-primary" />
                  ) : selected ? (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-filigran-ia-primary" />
                  ) : (
                    <Plus className="h-3.5 w-3.5 shrink-0 text-default-disabled" />
                  )}
                </button>
              );
              return locked ? (
                <Tip key={a.id} content={t_i18n("It can write, so it can read.")}>
                  <span className="block">{chip}</span>
                </Tip>
              ) : (
                chip
              );
            })}
          </div>
        )}
      </div>
    );
  };

  return (
    <div data-testid="coordination-access-panel" className="mt-3 rounded-sm border border-filigran-ia-primary/20 bg-filigran-ia-primary/5 p-3">
      <div className="mb-1 flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-filigran-ia-primary light:text-filigran-ia-primary" />
        <p className="content-compact font-medium">{t_i18n("Coordination access")}</p>
      </div>
      <p className="mb-2.5 content-caption text-default-secondary">
        {t_i18n("Which agents can read and change the company-managed work items this agent creates and the shared state it owns. By default everyone can read them and every company-managed agent can change them. The agent a work item is assigned to, and the agent that claimed it, can always work on it.")}
      </p>
      <div className="grid gap-2.5 sm:grid-cols-2">
        <div>
          <Select value={draft.read} onValueChange={(v) => onChange({ ...draft, read: v as Scope })}>
            <SelectLabel>{t_i18n("Can read")}</SelectLabel>
            <SelectTrigger data-testid="coordination-read-scope" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCOPES.map((scope) => (
                <SelectItem key={scope} value={scope} disabled={!readScopeAllowed(scope, draft.write)}>
                  {t_i18n(READ_LABELS[scope])}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Select value={draft.write} onValueChange={(v) => onChange(withWriteScope(draft, v as Scope))}>
            <SelectLabel>{t_i18n("Can change")}</SelectLabel>
            <SelectTrigger data-testid="coordination-write-scope" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCOPES.map((scope) => (
                <SelectItem key={scope} value={scope}>
                  {t_i18n(WRITE_LABELS[scope])}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      {draft.write === "agents" && picker("write_agent_ids", t_i18n("Agents that can change them"))}
      {draft.read === "agents" && picker("read_agent_ids", t_i18n("Agents that can read them"))}
    </div>
  );
}

/** The read-mode line: nothing while the agent keeps the default. */
export function CoordinationAccessSummary({ access, allAgents }: { access: CoordinationAccess | null | undefined; allAgents?: Agent[] }) {
  const { t_i18n } = useFormatter();
  if (!access) return null;
  const names = (ids: string[]) =>
    ids.map((id) => (allAgents ?? []).find((a) => a.id === id)?.name ?? t_i18n("agent")).join(", ");
  const describe = (scope: Scope, labels: typeof READ_LABELS | typeof WRITE_LABELS, ids: string[]) =>
    scope === "agents" && ids.length > 0 ? `${t_i18n(labels[scope])} (${names(ids)})` : t_i18n(labels[scope]);
  return (
    <p data-testid="coordination-access-summary" className="mt-2 content-caption text-default-secondary">
      <span className="text-default-secondary">{t_i18n("Coordination access:")} </span>
      {t_i18n("Can read")}: {describe(access.read, READ_LABELS, access.read_agent_ids)}
      {" - "}
      {t_i18n("Can change")}: {describe(access.write, WRITE_LABELS, access.write_agent_ids)}
    </p>
  );
}
