// SPDX-License-Identifier: Apache-2.0

/**
 * A random RFC 4122 version 4 UUID that works on any origin.
 *
 * `crypto.randomUUID()` exists only in a secure context (https, localhost):
 * on a plain http origin (a lab, an internal host served without TLS) it is
 * undefined and calling it throws "crypto.randomUUID is not a function",
 * which crashed `/integrations/new` before it rendered. `crypto.getRandomValues()`
 * is available in every context, so the UUID is built from 16 random bytes
 * when `randomUUID` is missing. Every frontend id goes through `randomId()`;
 * never call `crypto.randomUUID()` directly.
 */

interface RandomSource {
  getRandomValues(array: Uint8Array<ArrayBuffer>): unknown;
  randomUUID?: () => string;
}

const HEX = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, "0"));

/** A v4 UUID from 16 random bytes (version and variant bits set per RFC 4122). */
function uuidFromBytes(bytes: Uint8Array): string {
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const h = Array.from(bytes, (b) => HEX[b]);
  return `${h.slice(0, 4).join("")}-${h.slice(4, 6).join("")}-${h.slice(6, 8).join("")}-${h.slice(8, 10).join("")}-${h.slice(10, 16).join("")}`;
}

/**
 * `crypto.randomUUID()` when the context offers it, otherwise a v4 UUID built
 * from `crypto.getRandomValues()`. `source` is the crypto object to use (the
 * global one by default; a test passes its own).
 */
export function randomId(source: RandomSource = globalThis.crypto): string {
  if (typeof source.randomUUID === "function") return source.randomUUID();
  const bytes = new Uint8Array(16);
  source.getRandomValues(bytes);
  return uuidFromBytes(bytes);
}
