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
/** The typed control of an extra field, as the catalog declares it (`extra_field_inputs`). */
export type ExtraFieldInputSpec = {
  kind: string;
  minimum?: number | null;
  maximum?: number | null;
  default?: string | null;
  help?: string | null;
};

const DOMAIN_NAME = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/;

/** The entries of a stored domain list (one per line, or split by spaces, commas or semicolons). */
export function domainList(value: unknown): string[] {
  return typeof value === "string" ? value.split(/[\s,;]+/).filter(Boolean) : [];
}

/** Whether an entry is a domain name, read as the server reads it (a leading `@` and a trailing dot tolerated). */
function isDomainName(entry: string): boolean {
  return DOMAIN_NAME.test(entry.toLowerCase().replace(/^@/, "").replace(/\.$/, ""));
}

/**
 * What is wrong with a typed field's value: the domain entries that are not
 * domain names, or an integer outside its bounds. Blank is never wrong (the
 * default applies). Mirrors the backend's `invalid_extra_fields`.
 */
export function extraFieldProblem(
  value: unknown,
  spec: ExtraFieldInputSpec | undefined,
): { badDomains: string[] } | { outOfRange: true } | null {
  if (!spec) return null;
  if (spec.kind === "domains") {
    const badDomains = domainList(value).filter((entry) => !isDomainName(entry));
    return badDomains.length > 0 ? { badDomains } : null;
  }
  if (spec.kind === "integer") {
    const text = typeof value === "string" ? value.trim() : value == null ? "" : String(value);
    if (!text) return null;
    const number = Number(text);
    const low = spec.minimum ?? Number.NEGATIVE_INFINITY;
    const high = spec.maximum ?? Number.POSITIVE_INFINITY;
    return Number.isInteger(number) && number >= low && number <= high ? null : { outOfRange: true };
  }
  return null;
}

/** Whether any typed field of `values` breaks its rule, so the form refuses to save. */
export function hasInvalidExtraField(
  values: Record<string, unknown>,
  specs: Record<string, ExtraFieldInputSpec> | null | undefined,
): boolean {
  return Object.entries(specs ?? {}).some(([key, spec]) => extraFieldProblem(values[key], spec) !== null);
}

/** The prefilled values of the typed fields that declare a default. */
export function extraFieldDefaults(
  specs: Record<string, ExtraFieldInputSpec> | null | undefined,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(specs ?? {})
      .filter(([, spec]) => typeof spec.default === "string")
      .map(([key, spec]) => [key, spec.default as string]),
  );
}

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

/** A catalog label read as a form field: its name, its explanation and whether it may stay empty. */
export type ExtraFieldLabel = { name: string; detail: string | null; optional: boolean };

const OPTIONAL_LEAD = /^optional\b\s*(?:[\u2014\u2013,;:.-]\s*)?/i;
const REQUIRED_ONLY = /^required$/i;
/** Longest text read as a field's name; a longer one is an explanation. */
export const EXTRA_FIELD_NAME_MAX = 40;

/** Key words written as acronyms when a name is read off the key (`client_id` -> `Client ID`). */
const KEY_ACRONYMS: Readonly<Record<string, string>> = {
  api: "API", ca: "CA", db: "DB", dns: "DNS", es: "ES", http: "HTTP", id: "ID", ids: "IDs", ip: "IP",
  jwt: "JWT", oauth: "OAuth", pem: "PEM", sql: "SQL", sso: "SSO", ssl: "SSL", tls: "TLS", uri: "URI",
  url: "URL", urls: "URLs", waba: "WABA",
};

function capitalised(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/** A field's name read off its key: words of the key, acronyms kept (`phone_number_id` -> `Phone number ID`). */
export function extraFieldKeyName(key: string): string {
  const words = key.split(/[_\-\s]+/).filter(Boolean).map((word) => KEY_ACRONYMS[word.toLowerCase()] ?? word.toLowerCase());
  return words.length ? capitalised(words.join(" ")) : key;
}

function balanced(text: string): boolean {
  let depth = 0;
  for (const char of text) {
    if (char === "(") depth += 1;
    else if (char === ")" && --depth < 0) return false;
  }
  return depth === 0;
}

/** Index of the `(` that opens the label's last top-level parenthesis, when the label ends with it after a space. */
function trailingParenthesis(text: string): number | null {
  if (!text.endsWith(")")) return null;
  let depth = 0;
  for (let i = text.length - 1; i >= 0; i -= 1) {
    if (text[i] === ")") depth += 1;
    else if (text[i] === "(" && --depth === 0) return i > 0 && text[i - 1] === " " ? i : null;
  }
  return null;
}

/** Whether a text reads as a field's name: short, not a number, its parentheses balanced. */
function isFieldName(name: string): boolean {
  return name.length > 0 && name.length <= EXTRA_FIELD_NAME_MAX && !/^\d/.test(name) && balanced(name);
}

/**
 * Split a catalog label into the field's name and its explanation, so the name
 * is the label and the explanation is the info bubble. The catalog writes the
 * explanation after the name, in a balanced trailing parenthesis (`Webhook
 * secret (optional - for signature verification)`) or after a colon
 * (`Region: where the tenant is hosted`); `optional` at the start of the label
 * or of its explanation marks a field that may stay empty, `(required)` alone
 * adds nothing. When what precedes the explanation is not a name (a number, a
 * sentence, an unbalanced parenthesis, an `Optional - ...` label), the name is
 * read off the field's `key` and the whole label is the explanation.
 */
export function splitExtraFieldLabel(label: string, key?: string): ExtraFieldLabel {
  let text = label.trim();
  if (key && OPTIONAL_LEAD.test(text)) {
    text = text.replace(OPTIONAL_LEAD, "").trim();
    return { name: extraFieldKeyName(key), detail: text ? capitalised(text) : null, optional: true };
  }
  let name = text;
  let detail: string | null = null;
  const paren = trailingParenthesis(text);
  const colon = text.indexOf(": ");
  if (paren !== null && (colon < 0 || paren < colon)) {
    name = text.slice(0, paren).trim();
    detail = text.slice(paren + 1, -1).trim();
  } else if (colon > 0) {
    name = text.slice(0, colon).trim();
    detail = text.slice(colon + 2).trim();
  }
  let optional = false;
  if (detail !== null && OPTIONAL_LEAD.test(detail)) {
    optional = true;
    detail = detail.replace(OPTIONAL_LEAD, "").trim();
  }
  if (detail !== null && REQUIRED_ONLY.test(detail)) detail = "";
  // An explanation after a colon may end with its own `(optional, ...)`.
  if (!optional && detail) {
    const inner = trailingParenthesis(detail);
    if (inner !== null && OPTIONAL_LEAD.test(detail.slice(inner + 1))) optional = true;
  }
  if (!isFieldName(name)) {
    if (!key) return { name: text, detail: null, optional };
    // A bare marker leaves the text before it as the explanation; else the whole label is.
    const explanation = detail === "" ? name : text;
    return { name: extraFieldKeyName(key), detail: capitalised(explanation), optional };
  }
  return { name, detail: detail ? capitalised(detail) : null, optional };
}

/**
 * The labels of a provider's fields ({@link splitExtraFieldLabel}), each name
 * unique: a name shared by several fields (`Meta Cloud API: ...` on four of
 * them) is a group, not a name, so those fields are named off their key and
 * keep their whole label as the explanation.
 */
export function extraFieldLabels(fields: Readonly<Record<string, string>>): Record<string, ExtraFieldLabel> {
  const labels: Record<string, ExtraFieldLabel> = {};
  const uses = new Map<string, number>();
  for (const [key, label] of Object.entries(fields)) {
    labels[key] = splitExtraFieldLabel(label, key);
    const name = labels[key].name.toLowerCase();
    uses.set(name, (uses.get(name) ?? 0) + 1);
  }
  for (const [key, label] of Object.entries(fields)) {
    if ((uses.get(labels[key].name.toLowerCase()) ?? 0) > 1) {
      labels[key] = { name: extraFieldKeyName(key), detail: capitalised(label.trim()), optional: labels[key].optional };
    }
  }
  return labels;
}

/**
 * A field name inside a sentence ("Paste {field}..."): lower-case first letter,
 * kept when the name opens with an acronym (`API key`, `URL`) or is a proper
 * noun (every word capitalised: `Meta Cloud API`, `Azure AD Tenant ID`).
 */
export function fieldNameInSentence(name: string): string {
  if (name.length > 1 && /[A-Z]/.test(name.charAt(1))) return name;
  const words = name.split(/\s+/);
  if (words.length > 1 && words.every((word) => /^[A-Z0-9]/.test(word))) return name;
  return name.charAt(0).toLowerCase() + name.slice(1);
}
