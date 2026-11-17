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
 * Declared input parameters on a manual trigger — the UE "function
 * signature".
 *
 * Two storage formats coexist in `config.parameters`: the original KEY_VALUE
 * dict (`{name: default}`) and the typed list
 * (`[{name, type, required, default, description}]`). Every consumer (Run
 * dialog, node pins, the Call flow drawer) reads through this normaliser so
 * the two shapes never leak past this module. Mirrors the server's
 * `flow_runner.declared_parameters`.
 */

export type ParamType = "string" | "number" | "boolean" | "json";

export interface DeclaredParam {
  name: string;
  type: ParamType;
  required: boolean;
  default?: unknown;
  description?: string;
}

const PARAM_TYPES: readonly ParamType[] = ["string", "number", "boolean", "json"];

export function isParamType(value: unknown): value is ParamType {
  return typeof value === "string" && (PARAM_TYPES as readonly string[]).includes(value);
}

export function declaredParameters(
  config: Record<string, unknown> | undefined,
): DeclaredParam[] {
  const raw = config?.parameters;
  if (Array.isArray(raw)) {
    const out: DeclaredParam[] = [];
    for (const item of raw) {
      if (!item || typeof item !== "object") continue;
      const entry = item as Record<string, unknown>;
      const name = String(entry.name ?? "").trim();
      if (!name) continue;
      out.push({
        name,
        type: isParamType(entry.type) ? entry.type : "string",
        required: !!entry.required,
        default: entry.default,
        description:
          typeof entry.description === "string" ? entry.description : undefined,
      });
    }
    return out;
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, unknown>)
      .filter(([key]) => key.trim())
      .map(([key, value]) => ({
        name: key.trim(),
        type: "string" as const,
        required: false,
        default: value,
      }));
  }
  return [];
}

/** A parameter's default as the string an input field can edit. */
export function paramDefaultText(param: DeclaredParam): string {
  const value = param.default;
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}
