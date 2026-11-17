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
 * Drawer extras for declared input parameters — the UE "function
 * signature" editors.
 *
 * `ManualParamsExtras` (on `trigger.manual`) declares the signature: typed
 * rows (name / type / required / default). Each parameter also surfaces as a
 * pin on the node's right edge, and the Run dialog renders a typed form from
 * the same declaration.
 *
 * `FlowCallArguments` (on `flow.call`) is the calling side: when the called
 * flow's manual trigger declares parameters, one variable-aware input per
 * parameter appears here (stored under `config.arguments`), exactly like
 * calling a function in Blueprint shows one pin per declared input.
 */

import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useFlow } from "@/hooks/api/useFlowApi";
import { useFormatter } from "@/i18n";
import type { CanvasNode } from "./graph-adapter";
import {
  declaredParameters,
  isParamType,
  paramDefaultText,
  type DeclaredParam,
  type ParamType,
} from "./params";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";

const PARAM_TYPES: ParamType[] = ["string", "number", "boolean", "json"];

interface ManualParamsExtrasProps {
  node: CanvasNode;
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
}

export function ManualParamsExtras({
  node,
  readOnly,
  onChangeConfig,
}: ManualParamsExtrasProps) {
  const { t_i18n } = useFormatter();
  // The editor renders the RAW list, not declaredParameters(): that helper
  // drops unnamed entries (pins and the Run dialog must never see them), so
  // a freshly added row — always born nameless — would vanish before the
  // user can type its name.
  const rawParams = node.data.config?.parameters;
  const params: DeclaredParam[] = Array.isArray(rawParams)
    ? rawParams
        .filter(
          (item): item is Record<string, unknown> =>
            !!item && typeof item === "object",
        )
        .map((entry) => ({
          name: String(entry.name ?? ""),
          type: isParamType(entry.type) ? entry.type : "string",
          required: !!entry.required,
          default: entry.default,
        }))
    : [];

  const write = (next: DeclaredParam[]) =>
    onChangeConfig(
      "parameters",
      next.map((p) => ({
        name: p.name,
        type: p.type,
        required: p.required,
        ...(p.default !== undefined && p.default !== ""
          ? { default: p.default }
          : {}),
      })),
    );

  const update = (index: number, patch: Partial<DeclaredParam>) =>
    write(params.map((p, i) => (i === index ? { ...p, ...patch } : p)));

  return (
    <div className="space-y-1.5">
      <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
        {t_i18n("Input parameters")}
      </p>
      <p className="text-[11px] text-default-secondary">
        {t_i18n(
          "The run's typed signature: each parameter is asked for at run time, becomes a pin on the node, and Call flow nodes expose it as an argument.",
        )}
      </p>
      {params.map((param, index) => (
        <div key={index} className="space-y-1 rounded-sm border border-elevation-subtle p-2">
          <Input aria-label={t_i18n("parameter_name")}
            value={param.name}
            placeholder={t_i18n("parameter_name")}
            className="w-full font-mono-plex"
            disabled={readOnly}
            onChange={(e) => update(index, { name: e.target.value })}
          />
          <div className="flex items-center gap-1.5">
            <Select
              value={param.type}
              onValueChange={(v) =>
                isParamType(v) && update(index, { type: v })
              }
            >
              <SelectTrigger className="min-w-0 flex-1" aria-label={t_i18n("Type")} disabled={readOnly}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PARAM_TYPES.map((pt) => (
                  <SelectItem key={pt} value={pt}>
                    {pt}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Switch aria-label={t_i18n("Required")}
              checked={param.required}
              title={t_i18n("Required")}
              disabled={readOnly}
              onCheckedChange={(checked) => update(index, { required: checked })}
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label={t_i18n("Remove parameter")}
              className="shrink-0"
              disabled={readOnly}
              onClick={() => write(params.filter((_, i) => i !== index))}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
          <Input aria-label={t_i18n("Default value")}
            value={paramDefaultText(param)}
            placeholder={t_i18n("Default value")}
            disabled={readOnly}
            onChange={(e) =>
              update(index, { default: e.target.value || undefined })
            }
          />
        </div>
      ))}
      {!readOnly && (
        <Button
          variant="ghost"
          size="sm"
          className="gap-1 content-compact"
          onClick={() =>
            write([
              ...params,
              { name: "", type: "string", required: false },
            ])
          }
        >
          <Plus className="h-3 w-3" /> {t_i18n("Add parameter")}
        </Button>
      )}
    </div>
  );
}

interface FlowCallArgumentsProps {
  node: CanvasNode;
  /** The called flow's id — the component is only rendered when set. */
  flowId: string;
  suggestions: VariableSuggestion[];
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
}

export function FlowCallArguments({
  node,
  flowId,
  suggestions,
  readOnly,
  onChangeConfig,
}: FlowCallArgumentsProps) {
  const { t_i18n } = useFormatter();
  const { data: called } = useFlow(flowId);

  const manual = (called?.graph?.nodes ?? []).find(
    (n) => n.kind === "trigger.manual",
  );
  const params = manual ? declaredParameters(manual.config) : [];
  if (params.length === 0) return null;

  const args =
    node.data.config?.arguments && typeof node.data.config.arguments === "object"
      ? (node.data.config.arguments as Record<string, string>)
      : {};

  return (
    <div className="space-y-1.5">
      <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
        {t_i18n("Arguments")}
      </p>
      <p className="text-[11px] text-default-secondary">
        {t_i18n(
          "The called flow declares these parameters: values are checked against its signature before the sub-flow starts.",
        )}
      </p>
      {params.map((param) => (
        <div key={param.name} className="space-y-1">
          <Label className="flex items-center gap-1 font-mono-plex">
            {param.name}
            <span className="font-sans-plex content-caption text-default-secondary">
              ({param.type})
            </span>
            {param.required && <span className="text-feedback-error-primary">*</span>}
          </Label>
          <VariableInput
            aria-label={param.name}
            value={args[param.name] ?? ""}
            suggestions={suggestions}
            disabled={readOnly}
            placeholder={
              paramDefaultText(param) || `{{node.output}}`
            }
            onChange={(value) =>
              onChangeConfig("arguments", { ...args, [param.name]: value })
            }
          />
        </div>
      ))}
    </div>
  );
}
