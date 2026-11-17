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
 * The one-line summary a canvas node shows for its first required field, e.g.
 * "Agent: Alert Classifier" or "JSON Schema: severity, confidence".
 *
 * A value is read for what it is, never through `String()`: a JSON field
 * (the Structured output node's schema) used to show "[object Object]", and a
 * multi-select a raw id list instead of names.
 *
 * No React and no `@/` import, so `node-summary.test.ts` runs under
 * `node --test`.
 */

/** Past this, the summary is cut with an ellipsis: the card is one line. */
export const SUMMARY_MAX_CHARS = 60;

interface SummaryOption {
  value: string;
  label: string;
}

function truncate(text: string): string {
  return text.length > SUMMARY_MAX_CHARS ? `${text.slice(0, SUMMARY_MAX_CHARS - 1)}…` : text;
}

function labelOf(value: unknown, options: SummaryOption[]): string {
  const text = String(value);
  return options.find((o) => o.value === text)?.label ?? text;
}

/** The value as the card shows it, or `null` when there is nothing to show. */
export function summaryValue(raw: unknown, options: SummaryOption[] = []): string | null {
  if (raw === undefined || raw === null || raw === "") return null;
  if (Array.isArray(raw)) {
    const items = raw
      .map((item) =>
        item === undefined || item === null || item === ""
          ? null
          : typeof item === "object"
            ? summaryValue(item)
            : labelOf(item, options),
      )
      .filter((item): item is string => !!item);
    return items.length ? truncate(items.join(", ")) : null;
  }
  if (typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    // A JSON Schema reads as the fields it declares.
    const properties = record.properties;
    if (properties && typeof properties === "object" && !Array.isArray(properties)) {
      const names = Object.keys(properties);
      return names.length ? truncate(names.join(", ")) : null;
    }
    return Object.keys(record).length ? truncate(JSON.stringify(record)) : null;
  }
  return truncate(labelOf(raw, options));
}
