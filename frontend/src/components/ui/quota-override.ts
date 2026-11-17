// SPDX-License-Identifier: Apache-2.0

/**
 * What a user / group quota override means, mirrored from
 * `normalize_override_for_write` / `read_override_type_config` in
 * `backend/app/services/quota.py`:
 *
 * - no limit in it (no `default`, `platform_limit` or `per_model` entry): the
 *   override is empty and the user INHERITS from the group or the platform;
 * - `unlimited: true`: an explicit Unlimited, `default` kept `null`;
 * - anything else: the limits it carries (`0` is a hard stop).
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */
import type { QuotaTypeConfig } from "@/types";

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** True when the override carries a number: a default, a ceiling or a per-model limit. */
export function overrideHasLimit(cfg: QuotaTypeConfig | null | undefined): boolean {
  if (!cfg) return false;
  return (
    isNumber(cfg.default) ||
    isNumber(cfg.platform_limit) ||
    (cfg.per_model != null && Object.keys(cfg.per_model).length > 0)
  );
}

/** True when the override is the explicit Unlimited choice. */
export function isUnlimitedOverride(cfg: QuotaTypeConfig | null | undefined): boolean {
  return cfg?.unlimited === true;
}

/** Unlimited means no limit at all: no overall number, no per-model caps. */
function unlimitedForm(cfg: QuotaTypeConfig): QuotaTypeConfig {
  const form: QuotaTypeConfig = { ...cfg, unlimited: true, default: null };
  delete form.per_model;
  return form;
}

/** Turn the Unlimited switch on or off, keeping the period. */
export function setUnlimitedOverride(
  cfg: QuotaTypeConfig | null | undefined,
  unlimited: boolean,
): QuotaTypeConfig {
  const base: QuotaTypeConfig = { ...(cfg ?? {}), period: cfg?.period ?? "monthly" };
  if (unlimited) return unlimitedForm(base);
  const rest = { ...base };
  delete rest.unlimited;
  return rest;
}

/**
 * The override as it is saved: `null` (inherit) when it holds no limit and is
 * not Unlimited, the explicit Unlimited form otherwise, never a stray flag.
 */
export function normalizeQuotaOverride(
  cfg: QuotaTypeConfig | null | undefined,
): QuotaTypeConfig | null {
  if (!cfg) return null;
  if (isUnlimitedOverride(cfg)) return unlimitedForm(cfg);
  if (!overrideHasLimit(cfg)) return null;
  const rest = { ...cfg };
  delete rest.unlimited;
  return rest;
}
