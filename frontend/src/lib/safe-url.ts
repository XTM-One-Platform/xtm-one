// SPDX-License-Identifier: Apache-2.0

/**
 * A URL that is safe to put in an `href`: `http(s)` only. Any other scheme
 * (`javascript:`, `data:`, `vbscript:`, `file:`), a relative or
 * protocol-relative path, a string that is not a URL at all - or one carrying
 * userinfo (`https://trusted.example@evil.example` opens `evil.example`) - is
 * refused, so a value a third party controls is shown as text, never
 * followed: an agent's command in a machine-activity card, a registered
 * platform's address (the product switcher, the integration catalog), a
 * skill's or a document's source, a link in an agent's memory.
 */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.username || url.password ? null : url.href;
  } catch {
    return null;
  }
}
