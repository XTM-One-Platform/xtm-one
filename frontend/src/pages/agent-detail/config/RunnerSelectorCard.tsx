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
  MonitorCog, Monitor, CheckCircle2, Plus, X, Tag, Circle,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Input } from "@/components/ui/input";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useToast } from "@/components/ui/toast";
import { useRunners } from "@/hooks/api/useRunnerApi";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import type { Agent } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

/**
 * Agent runner-selector (fleet targeting, #plan Component 7).
 *
 * The counterpart to the MCP / integration binders: it scopes which enrolled
 * machines the agent's ``computer_use`` tools may drive. It only appears when
 * the ``computer_use`` built-in tool is enabled — a runner selection is
 * meaningless without it. Self-contained (fetches the fleet, saves via
 * ``useUpdateAgent``) so it slots in next to ``ToolingSection`` without
 * threading state through the parent.
 *
 * Selection semantics mirror the backend resolver:
 *   - empty ``runner_ids`` = ANY runner visible to the acting user.
 *   - a non-empty ``runner_ids`` = restrict to exactly those machines.
 *   - ``runner_labels`` narrows by label (AND with ``runner_ids``), so a
 *     durable selector ("audit") keeps working as machines are re-enrolled.
 */
export function RunnerSelectorCard({
  agent,
  canManage,
}: {
  agent: Agent;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // The viewer's machines plus the ones the agent targets (an admin on a
  // colleague's agent sees and keeps them).
  const { data: visibleRunners } = useRunners();
  const { data: everyRunner } = useRunners({ scope: "all" });
  const runners = useMemo(
    () => withBoundEntities(visibleRunners, everyRunner, agent.runner_ids),
    [visibleRunners, everyRunner, agent.runner_ids],
  );
  const update = useUpdateAgent();
  const aiOff = useAiActionDisabled();

  const [editing, setEditing] = useState(false);
  const [draftIds, setDraftIds] = useState<string[]>([]);
  const [draftLabels, setDraftLabels] = useState<string[]>([]);
  const [labelInput, setLabelInput] = useState("");

  const selectedIds = useMemo(() => agent.runner_ids || [], [agent.runner_ids]);
  const selectedLabels = useMemo(() => agent.runner_labels || [], [agent.runner_labels]);
  const selectedRunnerNames = useMemo(
    () =>
      selectedIds.map(
        (id) => runners?.find((r) => r.id === id)?.name ?? t_i18n("Unknown machine"),
      ),
    [selectedIds, runners, t_i18n],
  );

  // Only relevant when the agent can actually drive a machine. All hooks above
  // run unconditionally (rules-of-hooks); the guard is the first thing after.
  if (!(agent.builtin_tool_ids || []).includes("computer_use")) return null;

  const start = () => {
    setDraftIds([...selectedIds]);
    setDraftLabels([...selectedLabels]);
    setLabelInput("");
    setEditing(true);
  };
  const cancel = () => setEditing(false);

  const toggleRunner = (id: string) =>
    setDraftIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );

  const addLabel = () => {
    const v = labelInput.trim().toLowerCase();
    if (v && !draftLabels.includes(v)) setDraftLabels((p) => [...p, v]);
    setLabelInput("");
  };
  const removeLabel = (l: string) =>
    setDraftLabels((p) => p.filter((x) => x !== l));

  const save = () => {
    update.mutate(
      { id: agent.id, runner_ids: draftIds, runner_labels: draftLabels },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Runner selection saved"), variant: "success" });
          setEditing(false);
        },
        onError: (e: Error) =>
          toast({
            title: t_i18n("Could not save runner selection"),
            description: e.message,
            variant: "error",
          }),
      },
    );
  };

  const hasSelection = selectedIds.length > 0 || selectedLabels.length > 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <div>
          <CardTitle className="content-base flex items-center gap-2">
            <MonitorCog className="h-4 w-4 text-accent" /> {t_i18n("Runners")}
            {hasSelection && !editing && (
              <Badge variant="secondary" className="text-[9px] h-4 px-1.5">
                {selectedIds.length > 0
                  ? t_i18n("{count, plural, one {# machine} other {# machines}}", { count: selectedIds.length })
                  : t_i18n("by label")}
              </Badge>
            )}
          </CardTitle>
          <p className="content-caption text-default-secondary mt-0.5">
            {t_i18n("Which enrolled machines this agent's Computer Use tools may drive.")}
          </p>
        </div>
        <CardEditActions editing={editing} canEdit={canManage} onEdit={start} onCancel={cancel} onSave={save} saveDisabled={update.isPending || aiOff} aiGated />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        {editing ? (
          <div className="space-y-4">
            <p className="content-caption text-default-secondary">
              {t_i18n("Leave everything unselected to let the agent use any machine you can access. Select specific machines to restrict it, and/or add labels to target machines by role (kept working as machines are re-enrolled).")}
            </p>

            {/* Specific machines */}
            <div>
              <p className="content-caption text-default-secondary uppercase tracking-wider font-medium mb-2">
                {t_i18n("Specific machines")}
              </p>
              {runners && runners.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  {runners.map((r) => {
                    const selected = draftIds.includes(r.id);
                    const platform = RUNNER_PLATFORM_CONFIG[r.platform];
                    return (
                      <button
                        key={r.id}
                        type="button"
                        onClick={() => toggleRunner(r.id)}
                        className={cn(
                          "flex items-center gap-3 rounded-sm border p-3 text-left transition-all",
                          selected ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                        )}
                      >
                        <ProviderLogo
                          provider={r.platform}
                          logo={platform?.logo}
                          className={cn("h-5 w-5 shrink-0", !selected && "opacity-60")}
                        />
                        <div className="min-w-0 flex-1">
                          <p className={cn("content-base font-medium truncate", selected && "text-accent")}>{r.name}</p>
                          <p className="content-caption text-default-secondary flex items-center gap-1.5">
                            <Circle className={cn("h-2 w-2 fill-current", r.online ? "text-feedback-success-primary" : "text-default-disabled")} />
                            {platform?.label ?? r.platform}
                            {r.labels && r.labels.length > 0 && <span className="truncate">- {r.labels.join(", ")}</span>}
                          </p>
                        </div>
                        {selected ? (
                          <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                        ) : (
                          <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                        )}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="content-caption text-default-secondary italic">
                  {t_i18n("No machines enrolled yet. Install XTM One Desktop (or the headless runner) from the XTM One Desktop page.")}
                </p>
              )}
            </div>

            {/* Label selector */}
            <div>
              <p className="content-caption text-default-secondary uppercase tracking-wider font-medium mb-2">
                {t_i18n("Labels")}
              </p>
              <div className="flex flex-wrap items-center gap-1.5 mb-2">
                {draftLabels.map((l) => (
                  <span key={l} className="inline-flex items-center gap-1 rounded-sm bg-accent/10 text-accent content-compact px-2 py-0.5">
                    <Tag className="h-3 w-3" /> {l}
                    <button
                      type="button"
                      onClick={() => removeLabel(l)}
                      aria-label={t_i18n("Remove label {label}", { label: l })}
                      className="hover:text-feedback-error-primary"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                {draftLabels.length === 0 && (
                  <span className="content-caption text-default-secondary italic">{t_i18n("No label filter")}</span>
                )}
              </div>
              <div className="flex gap-2">
                <Input aria-label={t_i18n("Labels")}
                  value={labelInput}
                  onChange={(e) => setLabelInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addLabel(); } }}
                  placeholder={t_i18n("Add a label (e.g. audit)")}
                  className="h-8 content-compact"
                />
                <Button variant="outline" size="sm" className="h-8 content-compact gap-1" onClick={addLabel} disabled={!labelInput.trim()}>
                  <Plus className="h-3 w-3" /> {t_i18n("Add")}
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <div>
            {!hasSelection ? (
              <p className="content-base text-default-secondary italic py-2">
                {t_i18n("Any enrolled machine you can access.")}
              </p>
            ) : (
              <div className="space-y-2">
                {selectedIds.length > 0 && (
                  <div className="space-y-1.5">
                    {selectedRunnerNames.map((name, i) => {
                      const machine = runners?.find((r) => r.id === selectedIds[i]);
                      return (
                        <div key={selectedIds[i]} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                          {machine ? (
                            <ProviderLogo
                              provider={machine.platform}
                              logo={RUNNER_PLATFORM_CONFIG[machine.platform]?.logo}
                              className="h-4 w-4 shrink-0"
                            />
                          ) : (
                            <Monitor className="h-4 w-4 text-accent shrink-0" />
                          )}
                          <p className="content-base font-medium flex-1 truncate">{name}</p>
                          <Circle className={cn("h-2 w-2 fill-current shrink-0", machine?.online ? "text-feedback-success-primary" : "text-default-disabled")} />
                        </div>
                      );
                    })}
                  </div>
                )}
                {selectedLabels.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="content-caption text-default-secondary">{t_i18n("Labels:")}</span>
                    {selectedLabels.map((l) => (
                      <span key={l} className="inline-flex items-center gap-1 rounded-sm bg-accent/10 text-accent content-compact px-2 py-0.5">
                        <Tag className="h-3 w-3" /> {l}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
