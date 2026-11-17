/**
 * The Supabase Database access tab's logic: the draft read from and written
 * back to the integration's `extra_config` (`db_*` keys), and the probe body.
 *
 * No runtime import outside this folder's tree: `yarn test:unit` loads this
 * file under plain `node --test`, which does not resolve the `@/` alias.
 */
import type { SupabaseProbeRequest } from "@/types";
import { EXTRA_CONFIG_MASK, extraConfigPatch } from "../../../lib/extra-fields.ts";

/** The `extra_config` keys of a database role (`providers/supabase_postgres.py`). */
const DATABASE_KEYS = ["db_role", "db_password", "db_host", "db_port", "db_name", "db_ca_cert", "db_functions"] as const;

const DEFAULT_DATABASE = "postgres";
const DEFAULT_PORT = "5432";

export interface DatabaseDraft {
  projectRef: string;
  host: string;
  port: string;
  database: string;
  role: string;
  /** Typed here; empty = keep the stored one (when there is one). */
  password: string;
  caCert: string;
}

const text = (value: unknown): string => (typeof value === "string" ? value.trim() : value == null ? "" : String(value).trim());

/** The draft of a stored config. */
export function draftFromExtra(extra: Record<string, unknown> | null | undefined): DatabaseDraft {
  const config = extra ?? {};
  return {
    projectRef: text(config.project_ref),
    host: text(config.db_host),
    port: text(config.db_port) || DEFAULT_PORT,
    database: text(config.db_name) || DEFAULT_DATABASE,
    role: text(config.db_role),
    password: "",
    caCert: text(config.db_ca_cert),
  };
}

/** Whether a password is stored (the API answers it as the mask). */
export function hasStoredPassword(extra: Record<string, unknown> | null | undefined): boolean {
  return !!extra && extra.db_password === EXTRA_CONFIG_MASK;
}

/**
 * Whether the draft needs a typed password: the stored one is kept (and the
 * server sends it) only for the role, host and project it was saved for.
 */
export function needsPassword(extra: Record<string, unknown> | null | undefined, draft: DatabaseDraft): boolean {
  if (draft.password) return false;
  if (!hasStoredPassword(extra)) return true;
  const saved = draftFromExtra(extra);
  return (
    saved.role !== draft.role.trim() ||
    saved.host.toLowerCase() !== draft.host.trim().toLowerCase() ||
    saved.projectRef !== draft.projectRef.trim()
  );
}

/** The allowed functions of a stored config, `schema.name` each. */
export function allowedFunctions(extra: Record<string, unknown> | null | undefined): string[] {
  const raw = extra?.db_functions;
  const items = Array.isArray(raw) ? raw.map(String) : text(raw).split(/[,\n]/);
  return [...new Set(items.map((item) => item.trim()).filter(Boolean))];
}

/** The probe body of a draft: the typed password only, the stored one otherwise. */
export function probeBody(draft: DatabaseDraft): SupabaseProbeRequest {
  const body: SupabaseProbeRequest = {
    project_ref: draft.projectRef.trim(),
    db_host: draft.host.trim(),
    db_port: draft.port.trim(),
    db_name: draft.database.trim(),
    db_role: draft.role.trim(),
    db_ca_cert: draft.caCert.trim(),
  };
  if (draft.password) body.db_password = draft.password;
  return body;
}

/**
 * The `extra_config` patch an update merges: the other keys as they are, the
 * draft's, and the allowed functions. An untouched password is sent back as
 * the mask, which the update reads as "keep the stored one"; a stored key the
 * draft drops (an emptied CA certificate, a password for another target) is
 * sent as `null`, since the update keeps a key that is left out.
 */
export function extraFromDraft(
  extra: Record<string, unknown> | null | undefined,
  draft: DatabaseDraft,
  functions: readonly string[],
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...(extra ?? {}) };
  next.project_ref = draft.projectRef.trim();
  next.db_role = draft.role.trim();
  next.db_host = draft.host.trim().toLowerCase();
  next.db_port = draft.port.trim() || DEFAULT_PORT;
  next.db_name = draft.database.trim() || DEFAULT_DATABASE;
  next.db_functions = [...new Set(functions)].join(",");
  if (draft.password) next.db_password = draft.password;
  else if (!needsPassword(extra, draft)) next.db_password = EXTRA_CONFIG_MASK;
  else delete next.db_password;
  if (draft.caCert.trim()) next.db_ca_cert = draft.caCert.trim();
  else delete next.db_ca_cert;
  return extraConfigPatch(next, extra);
}

/**
 * The `extra_config` patch that removes the database role: each stored `db_*`
 * key sent as `null`, so the update drops it and the free SQL tools come back.
 */
export function withoutDatabaseRole(extra: Record<string, unknown> | null | undefined): Record<string, unknown> {
  const next: Record<string, unknown> = { ...(extra ?? {}) };
  for (const key of DATABASE_KEYS) delete next[key];
  return extraConfigPatch(next, extra);
}

/** A connection choice as one select value, and back. */
export function connectionValue(host: string, port: string | number): string {
  return `${host.trim().toLowerCase()}:${String(port).trim() || DEFAULT_PORT}`;
}

export function parseConnection(value: string): { host: string; port: string } {
  const index = value.lastIndexOf(":");
  if (index <= 0) return { host: value, port: DEFAULT_PORT };
  return { host: value.slice(0, index), port: value.slice(index + 1) || DEFAULT_PORT };
}

/** `schema.name` of a probed function. */
export const functionKey = (fn: { schema: string; name: string }): string => `${fn.schema}.${fn.name}`;
