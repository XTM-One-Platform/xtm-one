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
 * Drawer extras for the Integration node — callable operations.
 *
 * The agent runtime already declares every integration operation as a
 * ToolDefinition (name, description, JSON Schema of the arguments); this
 * component surfaces that catalogue in the builder so the node stops asking
 * for a free-text operation and a raw JSON blob:
 *
 * - **Operation** — a picker over the connected integration's operations,
 *   with each entry's description. An operation stored on the node that no
 *   longer exists still shows (flagged), so old flows stay editable.
 * - **Parameters** — one field per schema property: boolean → switch,
 *   enum → select, everything else a variable-aware input (templates like
 *   `{{node.output}}` welcome — the runner coerces primitives back against
 *   the schema at execution).
 *
 * Storage is UNCHANGED (`config.operation` string + `config.parameters`
 * object), so flows authored before this drawer keep running as-is.
 */

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFlowIntegrationOperations } from "@/hooks/api/useFlowApi";
import { useFormatter } from "@/i18n";
import type { FlowIntegrationOperation } from "@/types";
import type { CanvasNode } from "./graph-adapter";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";

interface IntegrationNodeExtrasProps {
  node: CanvasNode;
  /** The picked integration — the component only renders when set. */
  integrationId: string;
  suggestions: VariableSuggestion[];
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
  /** Merge several config keys in ONE commit — two sequential
   *  onChangeConfig calls would clobber each other (same snapshot). */
  onApplyConfigPatch?: (nodeId: string, patch: Record<string, unknown>) => void;
}

export function IntegrationNodeExtras({
  node,
  integrationId,
  suggestions,
  readOnly,
  onChangeConfig,
  onApplyConfigPatch,
}: IntegrationNodeExtrasProps) {
  const { t_i18n } = useFormatter();
  const { data: fetchedOperations, isLoading, error } =
    useFlowIntegrationOperations(integrationId);
  // A failed refetch keeps the last list; the refusal wins over it.
  const operations = error ? undefined : fetchedOperations;

  const operation =
    typeof node.data.config?.operation === "string"
      ? (node.data.config.operation as string)
      : "";
  const parameters =
    node.data.config?.parameters &&
    typeof node.data.config.parameters === "object" &&
    !Array.isArray(node.data.config.parameters)
      ? (node.data.config.parameters as Record<string, unknown>)
      : {};

  const selected = (operations ?? []).find((op) => op.name === operation);
  // A stored operation the provider no longer declares stays listed so the
  // node remains inspectable — flagged instead of silently swapped.
  const stale = operation && !isLoading && operations && !selected;

  const setParam = (key: string, value: unknown) =>
    onChangeConfig("parameters", { ...parameters, [key]: value });

  const renderParam = (
    name: string,
    spec: NonNullable<
      FlowIntegrationOperation["input_schema"]["properties"]
    >[string],
    required: boolean,
  ) => {
    const value = parameters[name];
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
            onCheckedChange={(checked) => setParam(name, checked)}
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
            onValueChange={(v) => setParam(name, v)}
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
              : (spec.default !== undefined
                  ? String(spec.default)
                  : "{{node.output}}")
          }
          onChange={(v) => setParam(name, v)}
        />
        {spec.description && (
          <p className="content-caption text-default-secondary">{spec.description}</p>
        )}
      </div>
    );
  };

  const properties = selected?.input_schema?.properties ?? {};
  const required = new Set(selected?.input_schema?.required ?? []);
  // Required parameters first — the ones the operation cannot run without.
  const paramNames = Object.keys(properties).sort(
    (a, b) => Number(required.has(b)) - Number(required.has(a)),
  );

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label>
          {t_i18n("Operation")} <span className="text-feedback-error-primary">*</span>
        </Label>
        <SearchableSelect
          aria-label={t_i18n("Operation")}
          options={(operations ?? []).map((op) => ({ value: op.name, label: op.name, keywords: [op.description] }))}
          pinned={stale ? [{ value: operation, label: `${operation} — ${t_i18n("no longer available")}` }] : []}
          value={operation}
          disabled={readOnly}
          placeholder={isLoading ? t_i18n("Loading operations…") : t_i18n("Choose...")}
          onValueChange={(v) => {
            // Switching operation resets the parameters: two operations
            // rarely share an argument shape, and stale keys would be sent.
            // ONE commit — sequential onChangeConfig calls clobber each other.
            if (onApplyConfigPatch) {
              onApplyConfigPatch(node.id, { operation: v, parameters: {} });
            } else {
              onChangeConfig("operation", v);
            }
          }}
        />
        {!isLoading && (operations ?? []).length === 0 && (
          error ? (
            <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
              {error.message}
            </p>
          ) : (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("This integration exposes no operations.")}
            </p>
          )
        )}
        {selected?.description && (
          <p className="text-[11px] text-default-secondary">
            {selected.description}
          </p>
        )}
      </div>

      {selected && (
        <div className="space-y-2">
          <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
            {t_i18n("Parameters")}
          </p>
          {paramNames.length === 0 && (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("This operation takes no parameters.")}
            </p>
          )}
          {paramNames.map((name) =>
            renderParam(name, properties[name], required.has(name)),
          )}
        </div>
      )}

      {stale && (
        <Badge
          variant="outline"
          className="border-feedback-alert-primary/40 content-caption text-feedback-warning-primary"
        >
          {t_i18n("This operation is no longer declared by the provider.")}
        </Badge>
      )}
    </div>
  );
}
