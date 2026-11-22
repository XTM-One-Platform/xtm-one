// SPDX-License-Identifier: Apache-2.0

/**
 * Keep the page being opened as the place the sign-in lands again: an answer
 * link opened from Teams or an email with no session goes to the sign-in page
 * and comes back to the link, SSO included. The sign-in page reads it with
 * `safeReturnPath` (`lib/session.ts`), the same key an ended session writes.
 */
import { stripBasePath } from "@/lib/base-path";
import { SESSION_RETURN_PATH_KEY } from "@/lib/session";

export function rememberReturnPath(): void {
  try {
    const { pathname, search, hash } = window.location;
    sessionStorage.setItem(SESSION_RETURN_PATH_KEY, `${stripBasePath(pathname)}${search}${hash}`);
  } catch {
    // Storage disabled: the person lands on the home page after signing in.
  }
}
