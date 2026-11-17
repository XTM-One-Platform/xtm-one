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
 * Drawer extras for the Call-tool node — direct tool invocation.
 *
 * Mirrors the Integration node's drawer: instead of a free-text tool name and
 * a raw JSON blob, a picker over the callable tools (flow-callable built-ins
 * + the caller's custom tools, grouped) and one field per schema property —
 * boolean → switch, enum → select, everything else a variable-aware input.
 * Picking a tool stores its schema on the node (`config.arguments_schema`) so
 * the runner coerces the string-typed form values back to what the tool
 * declares, and a custom tool's id next to its name (`config.tool_id`)
 * so the run executes that tool, whatever else later carries its name.
 *
 * With an LLM wired into the node's `llm` socket the drawer switches to LLM
 * mode: the author no longer picks THE tool but the set the model may
 * choose from, and the arguments form gives way to the candidates' signatures,
 * read-only — what the author sees offered is exactly what the model is
 * offered. The deterministic config is left untouched underneath, so unwiring
 * the LLM restores the form as it was.
 */

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { useFlowCallableTools } from "@/hooks/api/useFlowApi";
import { sortWithinGroups } from "@/lib/select-options";
import { MultiSelectListField } from "./MultiSelectListField";
import { useFormatter } from "@/i18n";
import type { FlowCallableTool } from "@/types";
import {
  candidateKey,
  pickCandidates,
  pinOf,
  readCandidates,
  shownTool,
  toolHints,
  toolKey,
} from "./call-tool-pins";
import type { CanvasNode } from "./graph-adapter";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";

interface ToolNodeExtrasProps {
  node: CanvasNode;
  suggestions: VariableSuggestion[];
  readOnly: boolean;
  /** True when a `capability.llm` node is wired into the `llm` socket. */
  llmMode?: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
  /** Merge several config keys in ONE commit — two sequential
   *  onChangeConfig calls would clobber each other (same snapshot). */
  onApplyConfigPatch?: (nodeId: string, patch: Record<string, unknown>) => void;
}

export function ToolNodeExtras({
  node,
  suggestions,
  readOnly,
  llmMode,
  onChangeConfig,
  onApplyConfigPatch,
}: ToolNodeExtrasProps) {
  const { t_i18n, locale } = useFormatter();
  const { data: tools, isLoading } = useFlowCallableTools();

  const toolName =
    typeof node.data.config?.tool_name === "string"
      ? (node.data.config.tool_name as string)
      : "";
  const args =
    node.data.config?.arguments &&
    typeof node.data.config.arguments === "object" &&
    !Array.isArray(node.data.config.arguments)
      ? (node.data.config.arguments as Record<string, unknown>)
      : {};

  const toolPin =
    typeof node.data.config?.tool_id === "string" && node.data.config.tool_id
      ? (node.data.config.tool_id as string)
      : null;

  const selected = shownTool(tools ?? [], toolName, toolPin);
  // A stored tool that is no longer offered stays listed so the node remains
  // inspectable — flagged instead of silently swapped.
  const stale = toolName && !isLoading && tools && !selected;

  const builtins = (tools ?? []).filter((t) => t.source === "builtin");
  const customs = (tools ?? []).filter((t) => t.source !== "builtin");
  // Tools sharing a name are all offered; where they do, each says whose it is.
  const hints = toolHints(tools ?? [], (tool: FlowCallableTool) => {
    switch (tool.origin) {
      case "own":
        return t_i18n("Your tool");
      case "company":
        return t_i18n("Company-managed");
      case "shared":
        return tool.owner_name
          ? t_i18n("Shared by {name}", { name: tool.owner_name })
          : t_i18n("Shared with you");
      default:
        return tool.owner_name
          ? t_i18n("Owned by {name}", { name: tool.owner_name })
          : t_i18n("Another user's tool");
    }
  });
  const originLabel = (tool: FlowCallableTool): string | null =>
    hints.get(toolKey(tool)) ?? null;

  const setArg = (key: string, value: unknown) =>
    onChangeConfig("arguments", { ...args, [key]: value });

  const renderArg = (
    name: string,
    spec: NonNullable<FlowCallableTool["input_schema"]["properties"]>[string],
    required: boolean,
  ) => {
    const value = args[name];
    const label = (
      <Label className="flex items-center gap-1 font-mono-plex">
        {name}
        <span className="font-sans-plex content-caption text-default-secondary">
          ({spec.type ?? "string"})
        </span>
        {required && <span className="text-feedback-error-primary">*</span>}
      </Label>
    );
    if (spec.type === "boolean") {
      return (
        <div key={name} className="flex items-center justify-between gap-2">
          {label}
          <Switch
            aria-label={name}
            checked={value === true || value === "true"}
            disabled={readOnly}
            onCheckedChange={(checked) => setArg(name, checked)}
          />
        </div>
      );
    }
    if (spec.enum && spec.enum.length > 0) {
      return (
        <div key={name} className="space-y-1">
          {label}
          <SearchableSelect
            aria-label={name}
            searchable="auto"
            sort={false}
            options={spec.enum.map((option) => ({ value: String(option), label: String(option) }))}
            value={value === undefined || value === null ? "" : String(value)}
            onValueChange={(v) => setArg(name, v)}
            disabled={readOnly}
            placeholder={t_i18n("Choose...")}
          />
          {spec.description && (
            <p className="content-caption text-default-secondary">
              {spec.description}
            </p>
          )}
        </div>
      );
    }
    return (
      <div key={name} className="space-y-1">
        {label}
        <VariableInput
          aria-label={name}
          value={value === undefined || value === null ? "" : String(value)}
          suggestions={suggestions}
          disabled={readOnly}
          placeholder={
            spec.type === "object" || spec.type === "array"
              ? '{"key": "value"}'
              : spec.default !== undefined
                ? String(spec.default)
                : "{{node.output}}"
          }
          onChange={(v) => setArg(name, v)}
        />
        {spec.description && (
          <p className="content-caption text-default-secondary">
            {spec.description}
          </p>
        )}
      </div>
    );
  };

  const properties = selected?.input_schema?.properties ?? {};
  const required = new Set(selected?.input_schema?.required ?? []);
  // Required arguments first — the ones the tool cannot run without.
  const argNames = Object.keys(properties).sort(
    (a, b) => Number(required.has(b)) - Number(required.has(a)),
  );

  // ── LLM mode ──────────────────────────────────────────────────────
  // The author declares the SET the model may choose from; the arguments are
  // the model's job, so the form gives way to the candidates' signatures.
  const candidateNames = Array.isArray(node.data.config?.candidate_tools)
    ? (node.data.config.candidate_tools as unknown[]).filter(
        (v): v is string => typeof v === "string" && !!v,
      )
    : [];

  if (llmMode) {
    const listed = tools ?? [];
    const options = listed.map((tool) => {
      const origin = originLabel(tool);
      return {
        value: toolKey(tool),
        label: origin ? `${tool.name} (${origin})` : tool.name,
        description: tool.description || null,
        icon: tool.source === "builtin" ? "Wrench" : "SquareCode",
      };
    });
    const picks = readCandidates(candidateNames, node.data.config?.candidate_tool_ids);
    const pickedKeys = picks.map((pick) => candidateKey(listed, pick));
    const chosen = picks
      .map((pick) => shownTool(listed, pick.name, pick.pin))
      .filter((tool): tool is FlowCallableTool => !!tool);
    // A stored candidate that no longer resolves is named, not dropped: the
    // runner skips it at run time and says so, and the author should see the
    // same thing here rather than a list that quietly shrank.
    const staleCandidates =
      isLoading || !tools
        ? []
        : picks
            .filter((pick) => !shownTool(tools, pick.name, pick.pin))
            .map((pick) => pick.name);

    return (
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label>
            {t_i18n("Tools the model may choose from")}{" "}
            <span className="text-feedback-error-primary">*</span>
          </Label>
          <MultiSelectListField
            options={options}
            value={pickedKeys}
            disabled={readOnly}
            // A candidate no longer listed gets a row of its own so it can be
            // removed; only once the tools loaded, or every one would be.
            showUnlisted={!isLoading && !!tools}
            onChange={(value) => {
              const next = pickCandidates(listed, picks, value);
              // The names and their pins in ONE commit.
              if (onApplyConfigPatch) {
                onApplyConfigPatch(node.id, next);
              } else {
                onChangeConfig("candidate_tools", next.candidate_tools);
              }
            }}
          />
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "The model may only call one of these: there is no 'any tool' option.",
            )}
          </p>
          {staleCandidates.length > 0 && (
            <p className="content-caption text-feedback-warning-primary">
              {t_i18n("No longer available:")} {staleCandidates.join(", ")}
            </p>
          )}
        </div>

        {chosen.length > 0 && (
          <div className="space-y-2">
            <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Signatures offered to the model")}
            </p>
            {chosen.map((tool) => {
              const properties = tool.input_schema?.properties ?? {};
              const requiredArgs = new Set(tool.input_schema?.required ?? []);
              const names = Object.keys(properties);
              return (
                <div
                  key={toolKey(tool)}
                  className="space-y-1 rounded-sm border border-elevation-subtle p-2"
                >
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono-plex content-compact">{tool.name}</span>
                    <Badge variant="outline" className="text-[9px]">
                      {tool.source === "builtin"
                        ? t_i18n("Built-in")
                        : t_i18n("Custom tools")}
                    </Badge>
                  </div>
                  {names.length === 0 ? (
                    <p className="content-caption text-default-secondary">
                      {t_i18n("This tool takes no arguments.")}
                    </p>
                  ) : (
                    <p className="font-mono-plex content-caption text-default-secondary">
                      {names
                        .map(
                          (n) =>
                            `${n}${requiredArgs.has(n) ? "*" : ""}: ${
                              properties[n]?.type ?? "string"
                            }`,
                        )
                        .join(", ")}
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

  const toolOption = (tool: FlowCallableTool, group: string): SelectOption => ({
    value: toolKey(tool),
    label: tool.label,
    description: originLabel(tool) || undefined,
    keywords: [tool.name, tool.description],
    group,
  });

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label>
          {t_i18n("Tool")} <span className="text-feedback-error-primary">*</span>
        </Label>
        <SearchableSelect
          aria-label={t_i18n("Tool")}
          options={sortWithinGroups(
            [
              ...builtins.map((tool) => toolOption(tool, t_i18n("Built-in"))),
              ...customs.map((tool) => toolOption(tool, t_i18n("Custom tools"))),
            ],
            (option) => option.group,
            (option) => option.label,
            locale,
          )}
          pinned={stale ? [{ value: toolName, label: `${toolName} — ${t_i18n("no longer available")}` }] : []}
          sort={false}
          value={selected ? toolKey(selected) : toolName}
          disabled={readOnly}
          placeholder={isLoading ? t_i18n("Loading tools…") : t_i18n("Choose...")}
          onValueChange={(v) => {
            const picked = (tools ?? []).find((t) => toolKey(t) === v);
            if (!picked) return;
            // Switching tool resets the arguments (shapes rarely match) and
            // stores the schema for runtime coercion and the custom tool's
            // id next to its name. ONE commit.
            if (onApplyConfigPatch) {
              onApplyConfigPatch(node.id, {
                tool_name: picked.name,
                tool_id: pinOf(picked),
                arguments: {},
                arguments_schema: picked.input_schema ?? null,
              });
            } else {
              onChangeConfig("tool_name", picked.name);
            }
          }}
        />
        {!isLoading && (tools ?? []).length === 0 && (
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "No callable tools available: for integration operations, use the Integration node.",
            )}
          </p>
        )}
        {selected?.description && (
          <p className="text-[11px] text-default-secondary">
            {selected.description}
          </p>
        )}
        {selected && (
          <Badge variant="outline" className="text-[9px]">
            {selected.source === "builtin"
              ? t_i18n("Built-in")
              : t_i18n("Custom tools")}
          </Badge>
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
          {argNames.map((name) =>
            renderArg(name, properties[name] ?? {}, required.has(name)),
          )}
        </div>
      )}
    </div>
  );
}
