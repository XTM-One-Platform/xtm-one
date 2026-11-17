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
 * The export log a deck download may carry.
 *
 * The server attaches what the exported file could not carry the way the
 * viewer shows it - a gauge, a map, or bars on two value axes, written as a picture, a chart it
 * could not read from the rendered page - as a JSON array of sentences in one
 * response header. The download itself is unaffected; the export menu reads
 * the header to tell the person what to expect when they open the file.
 * Mirrors `EXPORT_NOTES_HEADER` in `backend/app/api/v1/presentations.py`.
 * @public
 */

export const EXPORT_NOTES_HEADER = "X-Export-Notes";

/** The export log the server attached to a download, empty when it said nothing. */
export function exportNotes(headers: Headers): string[] {
  const raw = headers.get(EXPORT_NOTES_HEADER);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((note): note is string => typeof note === "string" && note.length > 0)
      : [];
  } catch {
    return [];
  }
}
