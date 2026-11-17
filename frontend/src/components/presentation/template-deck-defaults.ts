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
 * The `deck_defaults` object of a template's `extra_metadata`, read and
 * written from the Design editor (`TemplateDesignEditor.tsx`).
 *
 * The object holds two kinds of entries: the STRING deck keys the editor's
 * three selects drive (`theme`, `transition`, `aspect_ratio`) and the
 * per-cover OBJECTS (`hero_defaults`, `hero_tones`, `slide_themes`,
 * each keyed by `title` / `closing`) that a brand declares in the JSON. A
 * write from the editor changes ONE string key and must carry every other
 * entry through untouched: a duplicate of the seeded brand deck that had its
 * default transition changed here used to come out of the save without its
 * covers, because the object was rebuilt from the string entries alone.
 */

/** Every entry of `deck_defaults` as stored, `{}` when absent or malformed. */
export function readDeckDefaultsRaw(extraMetadata: Record<string, unknown>): Record<string, unknown> {
  const defaults = extraMetadata.deck_defaults;
  if (!defaults || typeof defaults !== "object" || Array.isArray(defaults)) return {};
  return { ...(defaults as Record<string, unknown>) };
}

/** The string-valued deck keys the editor's selects show. */
export function readDeckDefaults(extraMetadata: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(readDeckDefaultsRaw(extraMetadata)).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

/**
 * `extraMetadata` with ONE deck default set (or unset, on `null`) and every
 * other entry of `deck_defaults` - the per-cover objects included - kept as
 * it was; the key is dropped when nothing is left under it.
 */
export function withDeckDefault(
  extraMetadata: Record<string, unknown>,
  key: string,
  value: string | null,
): Record<string, unknown> {
  const nextDefaults = readDeckDefaultsRaw(extraMetadata);
  if (value) nextDefaults[key] = value;
  else delete nextDefaults[key];
  const next = { ...extraMetadata };
  if (Object.keys(nextDefaults).length > 0) next.deck_defaults = nextDefaults;
  else delete next.deck_defaults;
  return next;
}
