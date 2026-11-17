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
 * The checks of the units of work and rates forms (#9448), the same rules as
 * the server (`roi_records.validate_unit`, `validate_rate`), so a mistake is
 * said under its field before the request instead of as a 422 after it.
 * Every message is an English source string the caller translates.
 */

export interface MinutesDraft {
  low: string;
  likely: string;
  high: string;
  review: string;
}

export type MinutesField = keyof MinutesDraft;

/**
 * Every message of the checks, an English catalog key the caller translates.
 * @i18n-keys
 */
export const FORM_MESSAGES = {
  minutesRequired: "Enter a number of minutes.",
  minutesNegative: "Minutes cannot be negative.",
  likelyUnderLow: "At least the low estimate.",
  highUnderLikely: "At least the likely estimate.",
  reviewNotUnderLikely: "Less than the likely manual minutes.",
  keyShape: "2 to 64 characters: a to z, 0 to 9 and _.",
  currencyShape: "A three-letter currency code, such as EUR or USD.",
  amountRequired: "Enter an hourly rate.",
  amountNotPositive: "An hourly rate is above zero.",
  amountTooHigh: "At most 100,000 per hour.",
  nameRequired: "Enter a name.",
} as const;

const UNIT_KEY_PATTERN = /^[a-z0-9_]{2,64}$/;
const RATE_ROLE_PATTERN = /^[a-z0-9_]{2,64}$/;
const CURRENCY_PATTERN = /^[A-Z]{3}$/;

/** A number typed in a field, `null` when empty or not a number. */
export function parseNumber(raw: string): number | null {
  const trimmed = raw.trim().replace(",", ".");
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/** The first problem of each minutes field, keyed by field. */
export function minutesErrors(draft: MinutesDraft): Partial<Record<MinutesField, string>> {
  const errors: Partial<Record<MinutesField, string>> = {};
  const values = {
    low: parseNumber(draft.low),
    likely: parseNumber(draft.likely),
    high: parseNumber(draft.high),
    review: parseNumber(draft.review),
  };
  for (const field of ["low", "likely", "high", "review"] as const) {
    const value = values[field];
    if (value === null) errors[field] = FORM_MESSAGES.minutesRequired;
    else if (value < 0) errors[field] = FORM_MESSAGES.minutesNegative;
  }
  const { low, likely, high, review } = values;
  if (!errors.likely && low !== null && likely !== null && !errors.low && low > likely) {
    errors.likely = FORM_MESSAGES.likelyUnderLow;
  }
  if (!errors.high && likely !== null && high !== null && !errors.likely && likely > high) {
    errors.high = FORM_MESSAGES.highUnderLikely;
  }
  if (!errors.review && review !== null && likely !== null && !errors.likely && review >= likely) {
    errors.review = FORM_MESSAGES.reviewNotUnderLikely;
  }
  return errors;
}

/** A unit key derived from its name: lower case, a to z, 0 to 9 and _. */
export function unitKeyFromName(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 64);
}

export function unitKeyError(key: string): string | undefined {
  return UNIT_KEY_PATTERN.test(key) ? undefined : FORM_MESSAGES.keyShape;
}

export function rateRoleError(role: string): string | undefined {
  return RATE_ROLE_PATTERN.test(role) ? undefined : FORM_MESSAGES.keyShape;
}

export function currencyError(currency: string): string | undefined {
  return CURRENCY_PATTERN.test(currency) ? undefined : FORM_MESSAGES.currencyShape;
}

export function amountError(raw: string): string | undefined {
  const value = parseNumber(raw);
  if (value === null) return FORM_MESSAGES.amountRequired;
  if (value <= 0) return FORM_MESSAGES.amountNotPositive;
  if (value > 100_000) return FORM_MESSAGES.amountTooHigh;
  return undefined;
}

/** The roles a unit can be costed at: the built-in ones and those the organization gave a rate, each once, sorted. */
export function rateRoleChoices(builtin: readonly { role: string }[], organization: readonly { role: string }[]): string[] {
  return [...new Set([...builtin.map((r) => r.role), ...organization.map((r) => r.role)])].sort((a, b) => a.localeCompare(b));
}

/** The rate role a new unit starts on: the mode's fallback role when offered (`analyst` in XTM One mode, `knowledge_worker` in Copilot mode, #9550), else the first choice. */
export function defaultRateRole(choices: readonly string[]): string {
  for (const role of ["analyst", "knowledge_worker"]) if (choices.includes(role)) return role;
  return choices[0] ?? "analyst";
}
