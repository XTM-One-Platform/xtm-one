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

// ---------------------------------------------------------------------------
// Shared schema helpers (used by OverviewTab, ConfigTab, NewToolForm, BuiltinToolDetail)
// ---------------------------------------------------------------------------

import type { JsonSchema } from "@/types";
import { randomId } from "@/lib/random-id";

export interface SchemaProperty {
  name: string;
  type: string;
  description: string;
  required: boolean;
}

/**
 * UI type options shown in the parameter type dropdown.
 * File parameters are no longer declared in schemas — files are injected
 * as `$files` at runtime via FileContext.
 */
export const PARAM_TYPE_OPTIONS = ["string", "number", "boolean", "integer"] as const;

/**
 * Editable schema row with a stable per-row id for React keys — rows have no
 * natural identity (names are editable) and are removable, so index keys
 * would misassign input state after a removal.
 */
export type SchemaPropertyRow = SchemaProperty & { rowId: string };

export function newSchemaRow(): SchemaPropertyRow {
  return { name: "", type: "string", description: "", required: false, rowId: randomId() };
}

export function toSchemaRows(props: SchemaProperty[]): SchemaPropertyRow[] {
  return props.map((p) => ({ ...p, rowId: randomId() }));
}

export function parseSchemaProps(schema: JsonSchema | null): SchemaProperty[] {
  if (!schema) return [];
  const props = schema.properties ?? {};
  const required = schema.required ?? [];
  return Object.entries(props).map(([name, def]) => ({
    name,
    type: def.type || "string",
    description: def.description || "",
    required: required.includes(name),
  }));
}

export function buildSchema(properties: SchemaProperty[]): JsonSchema {
  const props: Record<string, { type: string; description: string }> = {};
  const required: string[] = [];
  for (const p of properties) {
    if (!p.name.trim()) continue;
    props[p.name.trim()] = { type: p.type, description: p.description };
    if (p.required) required.push(p.name.trim());
  }
  return { type: "object", properties: props, required };
}
