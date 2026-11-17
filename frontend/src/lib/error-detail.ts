// SPDX-License-Identifier: Apache-2.0

/**
 * What an API error's `detail` says: a string, `{message}`, or the list of
 * `{loc, msg}` a body model's refusal carries (a FastAPI 422), read as its
 * messages so a refused save says what was refused instead of "HTTP 422".
 *
 * No imports on purpose: `yarn test:unit` runs it under plain `node --test`.
 */

/** The distinct messages of a validation refusal's `detail` list, without pydantic's "Value error, " prefix. */
export function validationMessages(detail: unknown): string[] {
  if (!Array.isArray(detail)) return [];
  const messages = detail
    .map((d) => (d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string" ? (d as { msg: string }).msg : null))
    .filter((m): m is string => !!m)
    .map((m) => m.replace(/^Value error, /, ""));
  return Array.from(new Set(messages));
}

/** The message an API error is thrown with: what `detail` says, else `HTTP <status>`. */
export function errorDetailMessage(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  const messages = validationMessages(detail);
  return messages.length ? messages.join(" · ") : `HTTP ${status}`;
}
