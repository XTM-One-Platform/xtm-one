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

/** The text a JSON field shows for the value its node stores. */
export function jsonFieldText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}

/**
 * The value a JSON field stores for its text: the object, list or scalar the
 * text holds, or the text itself while it is not JSON (a draft, a bare
 * {{ref}}), which the server flags. Kept as text, a schema, a filter or a
 * payload is ignored at run time (`flow_graph.parse_json_fields`).
 */
export function jsonFieldValue(text: string): unknown {
  if (!text.trim()) return text;
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === "string" ? text : parsed;
  } catch {
    return text;
  }
}

/**
 * The text of a JSON field as typed, with the edit that stores its value.
 * Showing the stored value re-serialised would reformat under the cursor; a
 * change from elsewhere (undo, a reload) replaces the text.
 */
export function useJsonFieldDraft(
  value: unknown,
  onChange: (value: unknown) => void,
): readonly [string, (text: string) => void] {
  const stored = JSON.stringify(value ?? null);
  const [draft, setDraft] = useState(() => ({ text: jsonFieldText(value), stored }));
  let text = draft.text;
  if (draft.stored !== stored) {
    text = jsonFieldText(value);
    setDraft({ text, stored });
  }
  const edit = (next: string) => {
    const nextValue = jsonFieldValue(next);
    setDraft({ text: next, stored: JSON.stringify(nextValue ?? null) });
    onChange(nextValue);
  };
  return [text, edit] as const;
}
