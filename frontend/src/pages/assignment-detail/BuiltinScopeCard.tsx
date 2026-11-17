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
import { Filter, Sparkles } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { ToolScopeDialog } from "@/components/ui/tool-scope-dialog";
import { useToast } from "@/components/ui/toast";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { useFormatter } from "@/i18n";
import {
  builtinAllowedPayload,
  builtinFunctionRows,
  functionsOfferedToAssignment,
  scopedFunctions,
  withGroupScope,
  type BuiltinAllowedTools,
} from "@/lib/builtin-allowed-tools";
import type { Agent, Assignment } from "@/types";

// ---------------------------------------------------------------------------
// BuiltinScopeCard: the assignment's own per-function scoping of its agent's
// built-in groups. The server intersects it with the agent's scope,
// so each group only offers the functions the agent keeps; "All functions"
// leaves the group as the agent has it.
// ---------------------------------------------------------------------------

interface BuiltinScopeCardProps {
  assignment: Assignment;
  agent: Agent | undefined;
  canManage: boolean;
  onUpdate: () => void;
}

export function BuiltinScopeCard({ assignment, agent, canManage, onUpdate }: BuiltinScopeCardProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const aiOff = useAiActionDisabled();
  const { describe: describeBuiltinTool, isReady } = useBuiltinTools();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<BuiltinAllowedTools>({});
  const [dialogGroup, setDialogGroup] = useState<{ id: string; label: string } | null>(null);

  const agentScope = agent?.builtin_allowed_tools ?? null;
  // The agent's groups that leave a choice: more than one function offered.
  const groups = isReady
    ? (agent?.builtin_tool_ids ?? [])
        .map(describeBuiltinTool)
        .filter((tool) => tool.known && tool.available)
        .map((tool) => ({ tool, offered: functionsOfferedToAssignment(tool.functions, agentScope, tool.id) }))
        .filter(({ offered }) => offered.length > 1)
        .sort((a, b) => a.tool.label.localeCompare(b.tool.label))
    : [];
  if (isReady && groups.length === 0) return null;

  const shown = editing ? draft : (assignment.builtin_allowed_tools ?? {});
  const startEditing = () => { setDraft(assignment.builtin_allowed_tools ?? {}); setEditing(true); };
  const handleSave = () => {
    update.mutate(
      { id: assignment.id, builtin_allowed_tools: builtinAllowedPayload(draft, agent?.builtin_tool_ids ?? []) },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Built-in tool scope updated"), variant: "success" });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };
  const dialogOffered = dialogGroup
    ? groups.find(({ tool }) => tool.id === dialogGroup.id)?.offered ?? []
    : [];
  const agentKept = dialogGroup ? scopedFunctions(agentScope, dialogGroup.id) : null;

  return (
    <Card data-testid="assignment-builtin-scope-card">
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
        <CardTitle className="content-base flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("Built-in Tool Scope")}
        </CardTitle>
        <CardEditActions
          editing={editing}
          canEdit={canManage}
          onEdit={startEditing}
          onCancel={() => setEditing(false)}
          onSave={handleSave}
          saveDisabled={update.isPending || aiOff}
          aiGated
          testIds={{ edit: "assignment-builtin-scope-edit", save: "assignment-builtin-scope-save" }}
        />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        <p className="content-caption text-default-secondary mb-3">
          {t_i18n("Limit the built-in functions this assignment can call, within the agent's own scope.")}
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {groups.map(({ tool, offered }) => {
            const Icon = tool.icon;
            const scoped = scopedFunctions(shown, tool.id);
            const label = scoped === null
              ? t_i18n("All functions")
              : t_i18n("{selected} of {count, plural, one {# function} other {# functions}} selected", { selected: scoped.length, count: offered.length });
            return (
              <div key={tool.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                <Icon className="h-4 w-4 text-accent shrink-0" />
                <p className="content-base font-medium truncate min-w-0 flex-1">{tool.label}</p>
                {editing ? (
                  <button
                    type="button"
                    data-testid={`assignment-builtin-scope-${tool.id}`}
                    className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium shrink-0 transition-colors bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    onClick={() => setDialogGroup({ id: tool.id, label: tool.label })}
                  >
                    <Filter className="h-3 w-3" /> {label}
                  </button>
                ) : (
                  <span className="content-caption text-default-secondary shrink-0">{label}</span>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
      {dialogGroup && (
        <ToolScopeDialog
          tools={builtinFunctionRows(dialogOffered, t_i18n)}
          isLoading={!isReady}
          currentAllowed={scopedFunctions(draft, dialogGroup.id)}
          sourceLabel={dialogGroup.label}
          sourceType="built-in tool"
          contextLabel="assignment"
          unit="function"
          note={agentKept !== null
            ? t_i18n("Note: Only showing {count, plural, one {# function} other {# functions}} allowed by the parent agent's scoping.", { count: dialogOffered.length })
            : undefined}
          onSave={(allowed) => { setDraft((prev) => withGroupScope(prev, dialogGroup.id, allowed)); setDialogGroup(null); }}
          onClose={() => setDialogGroup(null)}
        />
      )}
    </Card>
  );
}
