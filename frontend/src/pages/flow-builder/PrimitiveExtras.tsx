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
 * Drawer extras for the Value node — the UE literal pin.
 *
 * A primitive carries a constant: either a typed literal entered here, or a
 * platform variable (the console's Variables, minus secrets — the value is
 * published into run journals). The control adapts to the declared type:
 * boolean → switch, number → numeric input, text/JSON → textarea.
 *
 * Storage: `config.value` (literal) XOR `config.variable_id` — switching
 * source clears the other in ONE commit so the two never fight.
 */

import { Label } from "@/components/ui/label";
import { useFlowEventTypes } from "@/hooks/api/useFlowApi";
import { DeclaredTypeCreator } from "./EventNodeExtras";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { cn } from "@/lib/utils";
import type { FlowFieldOption } from "@/types";
import type { CanvasNode } from "./graph-adapter";

interface PrimitiveExtrasProps {
  node: CanvasNode;
  /** The catalog's platform-variable options (secrets excluded server-side). */
  variableOptions: FlowFieldOption[];
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
  onApplyConfigPatch?: (nodeId: string, patch: Record<string, unknown>) => void;
}

export function PrimitiveExtras({
  node,
  variableOptions,
  readOnly,
  onChangeConfig,
  onApplyConfigPatch,
}: PrimitiveExtrasProps) {
  const { t_i18n } = useFormatter();
  const { data: declaredTypes } = useFlowEventTypes();

  const valueType =
    typeof node.data.config?.value_type === "string"
      ? (node.data.config.value_type as string)
      : "text";
  const literal =
    typeof node.data.config?.value === "string"
      ? (node.data.config.value as string)
      : "";
  const variableId =
    typeof node.data.config?.variable_id === "string"
      ? (node.data.config.variable_id as string)
      : "";
  const mode = variableId ? "variable" : "literal";
  const objectTypeId =
    typeof node.data.config?.object_type_id === "string"
      ? (node.data.config.object_type_id as string)
      : "";
  const objectFields =
    node.data.config?.object_fields &&
    typeof node.data.config.object_fields === "object"
      ? (node.data.config.object_fields as Record<string, string>)
      : {};
  const selectedType = (declaredTypes ?? []).find((t) => t.id === objectTypeId);

  const setSource = (next: string) => {
    if (next === mode) return;
    // ONE commit — clearing the other key in a second call would clobber.
    if (onApplyConfigPatch) {
      onApplyConfigPatch(
        node.id,
        next === "variable" ? { value: null } : { variable_id: null },
      );
    } else {
      onChangeConfig(next === "variable" ? "value" : "variable_id", null);
    }
  };

  if (valueType === "object") {
    // The UE struct pin: the constant conforms to a declared type — pick it,
    // fill its fields (coerced server-side), read {{node.value.<field>}}.
    return (
      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label>{t_i18n("Object type")}</Label>
          <SearchableSelect
            aria-label={t_i18n("Object type")}
            options={(declaredTypes ?? []).map((t) => ({ value: t.id, label: t.name }))}
            value={objectTypeId}
            disabled={readOnly}
            placeholder={t_i18n("Choose...")}
            onValueChange={(v) => {
              const picked = (declaredTypes ?? []).find((t) => t.id === v);
              if (onApplyConfigPatch) {
                onApplyConfigPatch(node.id, {
                  object_type_id: v,
                  object_type_name: picked?.name ?? null,
                  object_fields: {},
                  value: null,
                  variable_id: null,
                });
              } else {
                onChangeConfig("object_type_id", v);
              }
            }}
          />
        </div>
        <DeclaredTypeCreator
          readOnly={readOnly}
          buttonLabel={t_i18n("Declare a new type")}
          onCreated={(created) => {
            if (onApplyConfigPatch) {
              onApplyConfigPatch(node.id, {
                object_type_id: created.id,
                object_type_name: created.name,
                object_fields: {},
                value: null,
                variable_id: null,
              });
            } else {
              onChangeConfig("object_type_id", created.id);
            }
          }}
        />
        {selectedType &&
          (selectedType.fields ?? []).map((f) => (
            <div key={f.name} className="space-y-1.5">
              <Label>
                {f.name}
                <span className="ml-1 content-caption text-default-secondary">
                  {f.type}
                  {f.required ? " · required" : ""}
                </span>
              </Label>
              <Input aria-label={f.name}
                value={objectFields[f.name] ?? ""}
                disabled={readOnly}
                onChange={(e) =>
                  onChangeConfig("object_fields", {
                    ...objectFields,
                    [f.name]: e.target.value,
                  })
                }
              />
            </div>
          ))}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label>{t_i18n("Source")}</Label>
        <Select value={mode} onValueChange={setSource}>
          <SelectTrigger className="content-compact" disabled={readOnly}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="literal">{t_i18n("Literal value")}</SelectItem>
            <SelectItem value="variable">
              {t_i18n("Platform variable")}
            </SelectItem>
          </SelectContent>
        </Select>
      </div>

      {mode === "literal" ? (
        <div className="space-y-1.5">
          <Label>{t_i18n("Value")}</Label>
          {valueType === "boolean" ? (
            <Switch
              aria-label={t_i18n("Value")}
              checked={literal.trim().toLowerCase() === "true"}
              disabled={readOnly}
              onCheckedChange={(checked) =>
                onChangeConfig("value", checked ? "true" : "false")
              }
            />
          ) : valueType === "number" ? (
            <Input aria-label={t_i18n("Value")}
              type="number"
              value={literal}
              disabled={readOnly}
              onChange={(e) => onChangeConfig("value", e.target.value)}
            />
          ) : (
            <textarea
              rows={valueType === "json" ? 4 : 2}
              value={literal}
              disabled={readOnly}
              placeholder={valueType === "json" ? '{"key": "value"}' : undefined}
              className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 font-mono-plex content-compact placeholder:text-default-secondary", FIELD_FOCUS_RING)}
              onChange={(e) => onChangeConfig("value", e.target.value)}
            />
          )}
        </div>
      ) : (
        <div className="space-y-1.5">
          <Label>{t_i18n("Platform variable")}</Label>
          <SearchableSelect
            aria-label={t_i18n("Platform variable")}
            options={variableOptions.map((option) => ({
              value: option.value,
              label: option.label,
              keywords: [option.description],
            }))}
            value={variableId}
            onValueChange={(v) => onChangeConfig("variable_id", v)}
            disabled={readOnly}
            placeholder={t_i18n("Choose...")}
          />
          {variableOptions.length === 0 && (
            <p className="text-[11px] text-default-secondary">
              {t_i18n(
                "No platform variables available (secrets are never offered here).",
              )}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
