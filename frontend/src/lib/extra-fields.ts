// SPDX-License-Identifier: Apache-2.0

/**
 * Conditional visibility for an integration's extra credential fields.
 *
 * Some providers ask a question whose answer decides which fields apply — the
 * Google A2A mechanism authenticates either by impersonating a service account
 * or with a key JSON, never both. Presenting both fields and expecting the
 * operator to leave one empty is the version that produces a refusal instead
 * of a form, so the catalog declares the choice (`extra_field_options`) and
 * which field belongs to which answer (`extra_field_visible_when`).
 *
 * A hidden field is also OMITTED when saving. An update replaces the stored
 * extra config wholesale, so omitting it clears the credential that no longer
 * applies rather than leaving one stored and unused.
 */

export type ExtraFieldOption = { value: string; label: string };
export type VisibleWhen = Record<string, Record<string, string[]>>;

/**
 * The value a controlling field should be treated as having.
 *
 * The interesting case is an integration created BEFORE the choice existed: it
 * has no stored answer, and defaulting to the first option would hide the
 * credential it is actually using. So an absent answer is inferred from which
 * dependent field holds a value, and only falls back to the first option when
 * none does.
 */
export function deriveControllingValue(
  controlKey: string,
  values: Record<string, unknown>,
  visibleWhen?: VisibleWhen | null,
  options?: ExtraFieldOption[] | null,
): string {
  const stored = values[controlKey];
  if (typeof stored === "string" && stored.trim()) return stored.trim();

  for (const [field, condition] of Object.entries(visibleWhen ?? {})) {
    const allowed = condition?.[controlKey];
    if (!allowed?.length) continue;
    const value = values[field];
    const filled = typeof value === "string" ? value.trim().length > 0 : value != null;
    if (filled) return allowed[0];
  }
  return options?.[0]?.value ?? "";
}

/** Whether a field applies, given the answers currently selected. */
export function isExtraFieldVisible(
  key: string,
  values: Record<string, unknown>,
  visibleWhen?: VisibleWhen | null,
  optionsByKey?: Record<string, ExtraFieldOption[]> | null,
): boolean {
  const condition = visibleWhen?.[key];
  if (!condition) return true;
  return Object.entries(condition).every(([control, allowed]) =>
    allowed.includes(
      deriveControllingValue(control, values, visibleWhen, optionsByKey?.[control]),
    ),
  );
}

/** Drop the fields that do not apply, for the save payload. */
export function withoutHiddenExtraFields<T extends Record<string, unknown>>(
  values: T,
  visibleWhen?: VisibleWhen | null,
  optionsByKey?: Record<string, ExtraFieldOption[]> | null,
): Partial<T> {
  return Object.fromEntries(
    Object.entries(values).filter(([key]) =>
      isExtraFieldVisible(key, values, visibleWhen, optionsByKey),
    ),
  ) as Partial<T>;
}

/** Whether a value counts as "left empty" on the edit form. */
function isBlank(value: unknown): boolean {
  return value == null || (typeof value === "string" && value.trim().length === 0);
}

/**
 * Put back the stored value of every field the operator emptied.
 *
 * The form's payload is turned into a patch by {@link extraConfigPatch}, where
 * a stored key the payload no longer holds is removed; restoring an emptied
 * field keeps it. A secret
 * comes back as the redaction mask, which the endpoint reads as "keep".
 * Only the given keys are restored: the caller passes the VISIBLE fields,
 * because omitting a hidden one is what clears an abandoned credential.
 */
export function restoreEmptiedExtraFields<T extends Record<string, unknown>>(
  edited: T,
  stored: Record<string, unknown> | null | undefined,
  keys: Iterable<string>,
): T {
  const out: Record<string, unknown> = { ...edited };
  for (const key of keys) {
    if (!isBlank(out[key])) continue;
    const previous = stored?.[key];
    if (isBlank(previous)) continue;
    out[key] = previous;
  }
  return out as T;
}

/**
 * The `extra_config` an edit form sends: the update merges it key by key into
 * the stored config (a key left out is kept, `null` removes it), so every
 * stored key the form's final values no longer hold is sent as `null`. Saving
 * the form therefore stores exactly *values*, as it did when the update
 * replaced the config whole.
 */
export function extraConfigPatch(
  values: Record<string, unknown>,
  stored: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  const patch: Record<string, unknown> = { ...values };
  for (const key of Object.keys(stored ?? {})) {
    if (!(key in patch)) patch[key] = null;
  }
  return patch;
}

/** The API's stand-in for a stored secret. Mirrors `EXTRA_CONFIG_MASK` in
 * `app.api.v1.integrations`, which reads it back as "keep the stored value". */
export const EXTRA_CONFIG_MASK = "********";

/**
 * The starting values of a form over secrets the API reports as stored but
 * never returns: the mask for each, which a save sends back as "keep it".
 */
export function maskedStoredSecrets(storedSecretKeys: readonly string[] | undefined): Record<string, string> {
  return Object.fromEntries((storedSecretKeys ?? []).map((key) => [key, EXTRA_CONFIG_MASK]));
}

/**
 * Whether an extra-config key holds a secret. Mirrors the backend's
 * `_is_sensitive_setting_name`, so a form masks exactly what the API redacts:
 * secret / token / key / password names, except identifiers (`*_id`) and
 * endpoints (`*_url`, `*_urls`), plus `service_account_json`. A custom bundle
 * forces any key with `type: "secret"`.
 */
export function isSecretExtraKey(key: string, schemaType?: string | null): boolean {
  if (schemaType === "secret") return true;
  const lowered = key.toLowerCase();
  if (lowered === "service_account_json") return true;
  if (/_(id|urls?)$/.test(lowered)) return false;
  return /secret|key|token|password/.test(lowered);
}
