// SPDX-License-Identifier: Apache-2.0

/**
 * Secret vaults: what each provider type asks for, the form state of
 * Settings > Access and security > Secret vaults, and the `vault://<provider>/<path>#<key>`
 * references the secret picker writes. Pure: no React, no API.
 */
import type { SecretVaultGrant, SecretVaultProvider, SecretVaultProviderInput, SecretVaultType } from "../types/index.ts";

const VAULT_REFERENCE_PREFIX = "vault://";

/** Why a reference could not be read, mirrored from `REASON_DESCRIPTIONS` in `services/vault/errors.py`. @i18n-keys */
export const VAULT_FAILURE_REASONS = {
  provider_not_configured: "The reference names a vault provider this platform does not declare.",
  provider_disabled: "The reference names a vault provider an administrator disabled.",
  invalid_reference: "The stored reference is not a valid vault reference.",
  path_not_allowed: "The reference names a path outside the paths its vault provider allows.",
  not_found: "The vault has no such secret, or no such key in it.",
  key_required: "The secret holds several entries and the reference names none (#key).",
  access_denied: "The vault refused the platform's credentials for this secret.",
  unreachable: "The vault could not be reached or did not answer in time.",
  invalid_response: "The vault answered with something that is not a secret value.",
  misconfigured: "The vault provider's own configuration or credentials are missing or invalid.",
  variable_unavailable: "The secret variable it references is deleted, not secret, empty or no longer visible to the owner.",
} as const;

/** The provider types, in the order the dialog offers them. */
export const VAULT_TYPES: readonly SecretVaultType[] = ["hashicorp", "aws", "gcp", "azure", "onepassword"];

/** The brand mark of each type (`/public/logos`), root-relative: render it through `withBasePath()`. */
export const VAULT_TYPE_LOGOS: Record<SecretVaultType, string> = {
  hashicorp: "/logos/hashicorp_vault.svg",
  aws: "/logos/aws_security.svg",
  gcp: "/logos/google.svg",
  azure: "/logos/azure.svg",
  onepassword: "/logos/onepassword.svg",
};

/**
 * The sign-in methods of each type, the recommended one (a workload identity where the type has one) first.
 * HashiCorp's Kubernetes login posts the platform's service account token to the address: environment-declared only.
 */
export const VAULT_AUTH_METHODS: Record<SecretVaultType, readonly string[]> = {
  hashicorp: ["approle", "token"],
  aws: ["default_chain", "assume_role", "access_key"],
  gcp: ["workload_identity", "service_account_key"],
  azure: ["managed_identity", "client_secret"],
  onepassword: ["connect_token"],
};

export const VAULT_DEFAULT_METHOD: Record<SecretVaultType, string> = {
  hashicorp: "token",
  aws: "default_chain",
  gcp: "workload_identity",
  azure: "managed_identity",
  onepassword: "connect_token",
};

/** Methods that keep a long-lived cloud credential in the platform instead of using the workload's identity. */
export const STATIC_KEY_METHODS: ReadonlySet<string> = new Set(["access_key", "service_account_key", "client_secret"]);

type VaultFieldKind = "text" | "textarea" | "select" | "secret" | "secret-file";

export interface VaultFieldSpec {
  /** The setting or credential, by its lowercase name. */
  key: string;
  kind: VaultFieldKind;
  /** Stored encrypted and never served back (`credentials_set` says whether it is set). */
  credential?: boolean;
  required?: boolean;
  /** The sign-in methods it belongs to; every method when absent. */
  methods?: readonly string[];
  placeholder?: string;
  /** For a select: its values, shown as is. */
  options?: readonly string[];
  monospace?: boolean;
}

/** The fields of each type's form, in the order shown. */
const VAULT_FIELDS: Record<SecretVaultType, readonly VaultFieldSpec[]> = {
  hashicorp: [
    { key: "address", kind: "text", required: true, placeholder: "https://vault.example.com:8200", monospace: true },
    { key: "namespace", kind: "text", placeholder: "admin/team", monospace: true },
    { key: "token", kind: "secret", credential: true, required: true, methods: ["token"] },
    { key: "role_id", kind: "text", required: true, methods: ["approle"], monospace: true },
    { key: "secret_id", kind: "secret", credential: true, required: true, methods: ["approle"] },
    { key: "auth_mount", kind: "text", methods: ["approle"], placeholder: "approle", monospace: true },
    { key: "kv_version", kind: "select", options: ["2", "1"] },
    { key: "kv_mount", kind: "text", placeholder: "secret", monospace: true },
    { key: "ca_cert", kind: "textarea", placeholder: "-----BEGIN CERTIFICATE-----", monospace: true },
  ],
  aws: [
    { key: "region", kind: "select" },
    { key: "endpoint_url", kind: "text", placeholder: "https://vpce-....secretsmanager.eu-west-1.vpce.amazonaws.com", monospace: true },
    { key: "role_arn", kind: "text", required: true, methods: ["assume_role"], placeholder: "arn:aws:iam::123456789012:role/xtm-one-secrets", monospace: true },
    { key: "external_id", kind: "text", methods: ["assume_role"], monospace: true },
    { key: "access_key_id", kind: "text", required: true, methods: ["access_key"], placeholder: "AKIA...", monospace: true },
    { key: "secret_access_key", kind: "secret", credential: true, required: true, methods: ["access_key"] },
  ],
  gcp: [
    { key: "project", kind: "text", placeholder: "my-project", monospace: true },
    { key: "service_account_key", kind: "secret-file", credential: true, required: true, methods: ["service_account_key"] },
  ],
  azure: [
    { key: "vault_url", kind: "text", required: true, placeholder: "https://my-vault.vault.azure.net", monospace: true },
    { key: "tenant_id", kind: "text", methods: ["managed_identity", "client_secret"], monospace: true },
    { key: "client_id", kind: "text", methods: ["managed_identity", "client_secret"], monospace: true },
    { key: "client_secret", kind: "secret", credential: true, required: true, methods: ["client_secret"] },
  ],
  onepassword: [
    { key: "address", kind: "text", required: true, placeholder: "https://connect.example.com", monospace: true },
    { key: "token", kind: "secret", credential: true, required: true },
    { key: "ca_cert", kind: "textarea", placeholder: "-----BEGIN CERTIFICATE-----", monospace: true },
  ],
};

/** Fields required by a method beyond the ones marked so (Azure's client secret sign-in names its tenant and app). */
const REQUIRED_FOR_METHOD: Record<string, readonly string[]> = {
  client_secret: ["tenant_id", "client_id"],
};

/** The AWS regions offered for a Secrets Manager provider. */
export const AWS_REGIONS: readonly string[] = [
  "us-east-1",
  "us-east-2",
  "us-west-1",
  "us-west-2",
  "ca-central-1",
  "sa-east-1",
  "eu-west-1",
  "eu-west-2",
  "eu-west-3",
  "eu-central-1",
  "eu-central-2",
  "eu-north-1",
  "eu-south-1",
  "eu-south-2",
  "me-central-1",
  "me-south-1",
  "il-central-1",
  "af-south-1",
  "ap-east-1",
  "ap-south-1",
  "ap-south-2",
  "ap-northeast-1",
  "ap-northeast-2",
  "ap-northeast-3",
  "ap-southeast-1",
  "ap-southeast-2",
  "ap-southeast-3",
  "ap-southeast-4",
  "us-gov-east-1",
  "us-gov-west-1",
];

export type AccessMode = "admins" | "everyone" | "grants";

/** The provider dialog's state. Credentials hold only what was typed: an empty one is kept as stored. */
export interface VaultForm {
  name: string;
  displayName: string;
  type: SecretVaultType;
  method: string;
  settings: Record<string, string>;
  credentials: Record<string, string>;
  /** Credentials the administrator chose to clear. */
  cleared: string[];
  enabled: boolean;
  access: AccessMode;
  /** The path prefixes of the Everyone grant, one per entry. */
  everyonePaths: string[];
  grants: SecretVaultGrant[];
  browsable: boolean;
  cacheSeconds: string;
}

export function emptyVaultForm(type: SecretVaultType = "hashicorp"): VaultForm {
  return {
    name: "",
    displayName: "",
    type,
    method: VAULT_DEFAULT_METHOD[type],
    settings: type === "hashicorp" ? { kv_version: "2" } : {},
    credentials: {},
    cleared: [],
    enabled: true,
    access: "admins",
    everyonePaths: [],
    grants: [],
    browsable: false,
    cacheSeconds: "",
  };
}

/** The access mode a provider's grants read as: none (administrators only), one to everyone, or named groups and users. */
export function accessModeOf(grants: readonly SecretVaultGrant[]): AccessMode {
  if (grants.length === 0) return "admins";
  if (grants.length === 1 && grants[0].principal === "everyone") return "everyone";
  return "grants";
}

export function vaultFormFromProvider(provider: SecretVaultProvider): VaultForm {
  const { auth_method: method, ...settings } = provider.settings;
  const access = accessModeOf(provider.grants);
  return {
    name: provider.name,
    displayName: provider.display_name,
    type: provider.type,
    method: method || VAULT_DEFAULT_METHOD[provider.type],
    settings,
    credentials: {},
    cleared: [],
    enabled: provider.enabled,
    access,
    everyonePaths: access === "everyone" ? [...provider.grants[0].paths] : [],
    grants: access === "grants" ? provider.grants.map((grant) => ({ ...grant, paths: [...grant.paths] })) : [],
    browsable: provider.browsable,
    cacheSeconds: provider.cache_seconds === null ? "" : String(provider.cache_seconds),
  };
}

/** The fields the form shows for a type and a sign-in method. */
export function visibleFields(type: SecretVaultType, method: string): VaultFieldSpec[] {
  return VAULT_FIELDS[type].filter((field) => !field.methods || field.methods.includes(method));
}

/** The keys of the fields the form must fill for its type and sign-in method (the dialog marks them required). */
export function requiredVaultFields(form: Pick<VaultForm, "type" | "method" | "settings">): Set<string> {
  const byMethod = new Set(REQUIRED_FOR_METHOD[form.method] ?? []);
  const keys = new Set(visibleFields(form.type, form.method).filter((field) => field.required || byMethod.has(field.key)).map((field) => field.key));
  if (form.type === "hashicorp" && form.settings.kv_version === "1") keys.add("kv_mount");
  return keys;
}

/** Path prefixes as stored: trimmed, without leading or trailing slashes, empty and repeated ones dropped. */
function normalizePathList(entries: readonly string[]): string[] {
  return [...new Set(entries.map((entry) => entry.trim().replace(/^\/+|\/+$/g, "")).filter(Boolean))];
}

function grantsOf(form: VaultForm): SecretVaultGrant[] {
  if (form.access === "admins") return [];
  if (form.access === "everyone") return [{ principal: "everyone", id: null, paths: normalizePathList(form.everyonePaths) }];
  return form.grants.map(({ principal, id, paths }) => ({ principal, id, paths: normalizePathList(paths) }));
}

/** The API body of the form: the visible settings, the typed credentials, the cleared ones as `null`. */
export function vaultInputFromForm(form: VaultForm): SecretVaultProviderInput {
  const fields = visibleFields(form.type, form.method);
  const settings: Record<string, string | null> = { auth_method: form.method };
  const credentials: Record<string, string | null> = {};
  for (const field of fields) {
    if (field.credential) {
      const typed = form.credentials[field.key];
      if (typed) credentials[field.key] = typed;
      else if (form.cleared.includes(field.key)) credentials[field.key] = null;
    } else {
      const value = (form.settings[field.key] ?? "").trim();
      if (value) settings[field.key] = field.kind === "textarea" ? form.settings[field.key] : value;
    }
  }
  // A credential of a method no longer selected is cleared, so nothing stale stays stored.
  for (const field of VAULT_FIELDS[form.type]) {
    if (field.credential && !fields.includes(field)) credentials[field.key] = null;
  }
  const cache = form.cacheSeconds.trim();
  return {
    name: form.name.trim(),
    display_name: form.displayName.trim() || form.name.trim(),
    type: form.type,
    settings,
    credentials,
    enabled: form.enabled,
    grants: grantsOf(form),
    browsable: form.browsable,
    cache_seconds: cache === "" ? null : Number(cache),
  };
}

export const PROVIDER_NAME_RE = /^[a-z][a-z0-9_]{0,31}$/;

export type VaultFormProblem =
  | { kind: "name" }
  | { kind: "display_name" }
  | { kind: "required"; field: string }
  | { kind: "cache" }
  | { kind: "grant"; index: number };

/** What still stops the form from being saved; `stored` is the provider being edited (its set credentials count). */
export function vaultFormProblems(form: VaultForm, stored?: SecretVaultProvider | null): VaultFormProblem[] {
  const problems: VaultFormProblem[] = [];
  if (!PROVIDER_NAME_RE.test(form.name.trim())) problems.push({ kind: "name" });
  if (!form.displayName.trim() && !form.name.trim()) problems.push({ kind: "display_name" });
  const required = requiredVaultFields(form);
  for (const field of visibleFields(form.type, form.method)) {
    if (!required.has(field.key)) continue;
    const present = field.credential
      ? Boolean(form.credentials[field.key]) || (Boolean(stored?.credentials_set[field.key]) && !form.cleared.includes(field.key))
      : Boolean((form.settings[field.key] ?? "").trim());
    if (!present) problems.push({ kind: "required", field: field.key });
  }
  const cache = form.cacheSeconds.trim();
  if (cache !== "" && !(/^\d+$/.test(cache) && Number(cache) <= 86400)) problems.push({ kind: "cache" });
  if (form.access === "grants") {
    form.grants.forEach((grant, index) => {
      if (grant.principal !== "everyone" && !grant.id) problems.push({ kind: "grant", index });
    });
  }
  return problems;
}

/** A reference as the backend parses it (`app/core/secret_refs.py`). */
export interface VaultReference {
  provider: string;
  path: string;
  key: string | null;
}

const SEGMENT = String.raw`[A-Za-z0-9._~@:+=,\-]+`;
const PATH_RE = new RegExp(`^${SEGMENT}(?:/${SEGMENT})*$`);
const KEY_RE = /^[A-Za-z0-9._~@:+=,\-/]{1,256}$/;

export function isVaultReference(value: string | null | undefined): boolean {
  return typeof value === "string" && value.trim().toLowerCase().startsWith(VAULT_REFERENCE_PREFIX);
}

/** The reference *text* spells, or `null` when it is not a valid one. */
export function parseVaultReference(text: string | null | undefined): VaultReference | null {
  if (!isVaultReference(text)) return null;
  const body = (text as string).trim().slice(VAULT_REFERENCE_PREFIX.length);
  const hash = body.indexOf("#");
  const location = hash === -1 ? body : body.slice(0, hash);
  const key = hash === -1 ? null : body.slice(hash + 1);
  const slash = location.indexOf("/");
  if (slash === -1) return null;
  const provider = location.slice(0, slash);
  const path = location.slice(slash + 1);
  if (!PROVIDER_NAME_RE.test(provider) || !PATH_RE.test(path)) return null;
  if (path.split("/").some((segment) => segment === "." || segment === "..")) return null;
  if (key !== null && !KEY_RE.test(key)) return null;
  return { provider, path, key: key || null };
}

export function formatVaultReference(reference: VaultReference): string {
  return `${VAULT_REFERENCE_PREFIX}${reference.provider}/${reference.path.replace(/^\/+|\/+$/g, "")}${reference.key ? `#${reference.key}` : ""}`;
}

/** Whether *path* is inside one of *prefixes*, segment by segment, as the backend matches a grant. */
export function pathWithin(path: string, prefixes: readonly string[] | null): boolean {
  if (prefixes === null) return true;
  const segments = path.replace(/^\/+|\/+$/g, "").split("/");
  return prefixes.some((prefix) => {
    const parts = prefix.split("/");
    return parts.every((part, index) => segments[index] === part);
  });
}

/** The parent folder of a listing prefix (`a/b/` -> `a/`, `a/` -> ``). */
export function parentPrefix(prefix: string): string {
  const parts = prefix.replace(/\/+$/, "").split("/").filter(Boolean);
  parts.pop();
  return parts.length ? `${parts.join("/")}/` : "";
}

const RECENT_KEY = "xtm-vault-recent-paths";
const RECENT_LIMIT = 6;

/** The paths picked lately, most recent first, per provider (in `localStorage`; paths only, never a value). */
export function recentVaultPaths(provider: string, storage: Pick<Storage, "getItem"> | null): string[] {
  try {
    const all = JSON.parse(storage?.getItem(RECENT_KEY) ?? "{}") as Record<string, unknown>;
    const paths = all[provider];
    return Array.isArray(paths) ? paths.filter((path): path is string => typeof path === "string").slice(0, RECENT_LIMIT) : [];
  } catch {
    return [];
  }
}

export function rememberVaultPath(provider: string, path: string, storage: Pick<Storage, "getItem" | "setItem"> | null): void {
  if (!storage) return;
  try {
    const all = JSON.parse(storage.getItem(RECENT_KEY) ?? "{}") as Record<string, string[]>;
    const current = Array.isArray(all[provider]) ? all[provider] : [];
    all[provider] = [path, ...current.filter((existing) => existing !== path)].slice(0, RECENT_LIMIT);
    storage.setItem(RECENT_KEY, JSON.stringify(all));
  } catch {
    // A full or blocked storage only costs the shortcut.
  }
}
