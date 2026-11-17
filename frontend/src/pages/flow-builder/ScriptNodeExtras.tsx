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
 * Drawer extras for the Script node — a Custom tool bound to a flow.
 *
 * Deliberately NOT a new scripting surface: the node binds a platform
 * Custom tool (the same object agents call), so the script is written once
 * and reused everywhere. The drawer offers pick-or-create: a picker over the
 * caller's visible Custom tools, one argument field per declared input
 * (rendered from the tool's LIVE input_schema), and an inline creator —
 * the DeclaredTypeCreator pattern — that posts through useCreateCustomTool
 * and binds the fresh tool. Editing the code happens in Tools, where the
 * full editor (test runs, sharing, versioning) already lives.
 */

import { useMemo, useState } from "react";
import { ExternalLink, Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";
import type { CanvasNode } from "./graph-adapter";
import { ScriptCreatorDialog } from "./ScriptCreatorDialog";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";
import { withBasePath } from "@/lib/base-path";

interface ScriptNodeExtrasProps {
  node: CanvasNode;
  suggestions: VariableSuggestion[];
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
  /** Merge several config keys in ONE commit (same contract as ToolNodeExtras). */
  onApplyConfigPatch?: (nodeId: string, patch: Record<string, unknown>) => void;
}

export function ScriptNodeExtras({
  node,
  suggestions,
  readOnly,
  onChangeConfig,
  onApplyConfigPatch,
}: ScriptNodeExtrasProps) {
  const { t_i18n } = useFormatter();
  const [creatorOpen, setCreatorOpen] = useState(false);

  const toolId =
    typeof node.data.config?.custom_tool_id === "string"
      ? (node.data.config.custom_tool_id as string)
      : "";
  // The viewer's tools plus the one the node runs, so an admin editing a
  // colleague's flow sees its tool instead of a stale reference.
  const { data: visibleTools, isLoading } = useCustomTools();
  const { data: allTools } = useCustomTools({ scope: "all" });
  const tools = useMemo(
    () => withBoundEntities(visibleTools, allTools, toolId ? [toolId] : []),
    [visibleTools, allTools, toolId],
  );
  const args =
    node.data.config?.arguments &&
    typeof node.data.config.arguments === "object" &&
    !Array.isArray(node.data.config.arguments)
      ? (node.data.config.arguments as Record<string, unknown>)
      : {};

  const selected = (tools ?? []).find((t) => t.id === toolId);
  const stale = toolId && !isLoading && tools && !selected;

  const bindTool = (id: string) => {
    // Switching tool resets the arguments — shapes rarely match. ONE commit.
    if (onApplyConfigPatch) {
      onApplyConfigPatch(node.id, { custom_tool_id: id, arguments: {} });
    } else {
      onChangeConfig("custom_tool_id", id);
    }
  };

  const setArg = (key: string, value: unknown) =>
    onChangeConfig("arguments", { ...args, [key]: value });

  const properties =
    (selected?.input_schema?.properties as
      | Record<string, { type?: string; description?: string; enum?: unknown[] }>
      | undefined) ?? {};
  const required = new Set(
    (selected?.input_schema?.required as string[] | undefined) ?? [],
  );
  const argNames = Object.keys(properties).sort(
    (a, b) => Number(required.has(b)) - Number(required.has(a)),
  );

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label>
          {t_i18n("Custom tool")} <span className="text-feedback-error-primary">*</span>
        </Label>
        <SearchableSelect
          aria-label={t_i18n("Custom tool")}
          options={(tools ?? []).map((tool) => ({ value: tool.id, label: tool.name, keywords: [tool.description] }))}
          pinned={stale ? [{ value: toolId, label: `${toolId.slice(0, 8)}… — ${t_i18n("no longer available")}` }] : []}
          value={toolId}
          onValueChange={bindTool}
          disabled={readOnly}
          placeholder={isLoading ? t_i18n("Loading tools…") : t_i18n("Choose...")}
        />
        {selected && (
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-[9px]">
              {selected.language === "python" ? "Python" : "JavaScript"}
            </Badge>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-[11px] text-default-secondary underline-offset-2 hover:underline"
              onClick={() => window.open(withBasePath(`/tools/${selected.id}`), "_blank", "noopener")}
            >
              <ExternalLink className="h-3 w-3" />
              {t_i18n("Edit the script in Tools")}
            </button>
          </div>
        )}
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1.5 content-compact"
          disabled={readOnly}
          onClick={() => setCreatorOpen(true)}
        >
          <Plus className="h-3.5 w-3.5" />
          {t_i18n("Write a new script")}
        </Button>
        {creatorOpen && (
          <ScriptCreatorDialog
            onClose={() => setCreatorOpen(false)}
            onCreated={(c) => bindTool(c.id)}
          />
        )}
      </div>

      {selected && (
        <div className="space-y-2">
          <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
            {t_i18n("Arguments")}
          </p>
          {argNames.length === 0 && (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("This tool takes no arguments.")}
            </p>
          )}
          {argNames.map((name) => {
            const spec = properties[name] ?? {};
            const value = args[name];
            return (
              <div key={name} className="space-y-1">
                <Label className="flex items-center gap-1 font-mono-plex">
                  {name}
                  <span className="font-sans-plex content-caption text-default-secondary">
                    ({spec.type ?? "string"})
                  </span>
                  {required.has(name) && <span className="text-feedback-error-primary">*</span>}
                </Label>
                <VariableInput
                  aria-label={name}
                  value={value === undefined || value === null ? "" : String(value)}
                  suggestions={suggestions}
                  disabled={readOnly}
                  placeholder="{{node.output}}"
                  onChange={(v) => setArg(name, v)}
                />
                {spec.description && (
                  <p className="content-caption text-default-secondary">
                    {spec.description}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
