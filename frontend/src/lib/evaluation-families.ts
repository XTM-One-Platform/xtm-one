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
 * The evaluation families' copy, translated.
 *
 * `GET /evaluations/families` serves each family's label and description in English
 * (`backend/app/services/evaluation/metric_package/families.py`), and that text stays
 * the source: it is the catalog key, not a local copy. A sentence the server changes
 * reads in English until it is translated, never as the stale one.
 */

/** The server's current family copy, listed so `validate-i18n` requires a translation of
 * each. Keep in step with `families.py`. @i18n-keys */
export const FAMILY_CATALOG_COPY = [
  "STIX Bundle",
  "STIX 2.1 bundles, compared with a reference bundle object by object and relationship by relationship, sightings included.",
  "JSON",
  "Any JSON answer (analysis, detection rules, structured extractions) compared with a reference object field by field, each field as its schema declares it.",
  "Classification",
  "Multi-label classification, compared with a reference label set.",
  "Prose",
  "Natural-language prose, compared with a reference answer. Comparison ignores case and the punctuation around a word (a trailing period, quotes, brackets, bold), keeps the punctuation inside it (evil.com, T1059.001), and splits words on whitespace: scripts that do not use spaces (Chinese, Japanese, Thai) compare as a single token, so the graded metrics behave like exact match there.",
] as const;

type Translate = (id: string) => string;

/** What these helpers read of a catalog entry (`FamilyInfo`, or a narrower view of it). */
interface FamilyCopy {
  id: string;
  label: string;
  description?: string;
}

/** A family's label in the reader's language, or the raw id for a family the catalog lacks. */
export function familyLabel(
  family: string,
  catalog: FamilyCopy[] | undefined,
  t_i18n: Translate,
): string {
  const meta = catalog?.find((f) => f.id === family);
  return meta ? t_i18n(meta.label) : family;
}

/** A family's description in the reader's language, or `null` without one. */
export function familyDescription(meta: FamilyCopy | undefined, t_i18n: Translate): string | null {
  return meta?.description ? t_i18n(meta.description) : null;
}
