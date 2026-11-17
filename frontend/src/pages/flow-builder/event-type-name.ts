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
 * The event type name rule, mirrored from the backend's
 * `EventTypeName` (`app/api/v1/schemas/flow.py`) so the declare form never
 * offers Create for a name the API refuses with a bare 422.
 *
 * Pure helpers only (`node --test`).
 */

/** The `flow_event_types.name` column is `VARCHAR(120)`. */
export const EVENT_TYPE_NAME_MAX_LENGTH = 120;

// What JavaScript's `\s` leaves out of the backend's padding set: the
// separators and NEL that Python's `str.isspace()` counts as whitespace, and
// the zero-width characters, which are not whitespace at all - a name made
// only of them is as invisible as an empty one.
const EXTRA_PADDING = new Set([
  "\u001C",
  "\u001D",
  "\u001E",
  "\u001F",
  "\u0085",
  "\u200B",
  "\u200C",
  "\u200D",
  "\u2060",
]);

function isPadding(char: string): boolean {
  return /\s/.test(char) || EXTRA_PADDING.has(char);
}

/** The name as the API stores it: padding stripped from both ends only. */
export function normalizeEventTypeName(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && isPadding(value[start])) start += 1;
  while (end > start && isPadding(value[end - 1])) end -= 1;
  return value.slice(start, end);
}
