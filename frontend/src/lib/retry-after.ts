// SPDX-License-Identifier: Apache-2.0

/**
 * The seconds an HTTP `Retry-After` header asks to wait: delay-seconds or an
 * HTTP date (RFC 9110 10.2.3). `undefined` when absent, unreadable or already
 * past.
 *
 * No imports on purpose: `yarn test:unit` runs it under plain `node --test`.
 */
export function parseRetryAfterSeconds(value: string | null | undefined, now: number = Date.now()): number | undefined {
  const text = (value ?? "").trim();
  if (!text) return undefined;
  if (/^\d+$/.test(text)) {
    const seconds = Number(text);
    return seconds > 0 ? seconds : undefined;
  }
  const at = Date.parse(text);
  if (Number.isNaN(at) || !/[a-z]/i.test(text)) return undefined;
  const seconds = Math.ceil((at - now) / 1000);
  return seconds > 0 ? seconds : undefined;
}
