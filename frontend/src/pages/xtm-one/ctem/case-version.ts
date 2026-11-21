// SPDX-License-Identifier: Apache-2.0

/**
 * The version a CTEM case targets (CTEM-11, #946 in the backend, shown by
 * #928). The version knowledge itself is OpenCTI's and reaches the page as
 * fields of each issue, read with the viewer's own credentials
 * (dev-docs/ctem-cases.md section 4.2bis); this module only reads the version
 * of an upgrade key, which a `retargeted` history row carries.
 */

/** The version of an upgrade key, `upgrade:<vendor>/<product>@<version>`; null for `@latest` or another key. */
export function keyVersion(key: string | null): string | null {
  if (!key?.startsWith("upgrade:") || !key.includes("@")) return null;
  const version = key.slice(key.lastIndexOf("@") + 1);
  return version && version !== "latest" ? version : null;
}
