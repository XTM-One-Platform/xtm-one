// SPDX-License-Identifier: Apache-2.0

/**
 * The version a CTEM case targets, read from what the case already carries
 * (CTEM-11, #894 in the backend, shown by #928): the version of a `retargeted`
 * key, and the members the recompute closed because the version the
 * organization declares is outside every affected range
 * (dev-docs/ctem-cases.md section 4.2bis). The backend's evidence sentence is
 * read for its version only and drawn through a catalogued sentence.
 */

/** The evidence the recompute writes on such a member (backend ctem_fixed_version.NOT_AFFECTED_PREFIX). */
const NOT_AFFECTED = /^not affected: version (.+) is outside the affected ranges$/;

/** The version of an upgrade key, `upgrade:<vendor>/<product>@<version>`; null for `@latest` or another key. */
export function keyVersion(key: string | null): string | null {
  if (!key?.startsWith("upgrade:") || !key.includes("@")) return null;
  const version = key.slice(key.lastIndexOf("@") + 1);
  return version && version !== "latest" ? version : null;
}

/** The declared version a "not affected" evidence names, else null. */
export function notAffectedVersion(evidence: string | null): string | null {
  return evidence?.trim().match(NOT_AFFECTED)?.[1] ?? null;
}

/** Each member whose last close said "not affected", with the declared version; a later close at the source clears it. */
export function notAffectedVersions(history: readonly { kind: string; issue: string | null; evidence: string | null }[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const e of history) {
    if (e.kind !== "issue_closed" || !e.issue) continue;
    const version = notAffectedVersion(e.evidence);
    if (version) out.set(e.issue, version);
    else out.delete(e.issue);
  }
  return out;
}
