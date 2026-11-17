// SPDX-License-Identifier: Apache-2.0

/**
 * How long a delete confirmation waits for the sharing preview. The
 * dialog is on screen from the click with the page's own text; Confirm waits
 * for the preview's access-loss details, and past this bound the page's text
 * stands and Confirm is enabled, so a slow preview never holds a delete back.
 *
 * Dependency-free on purpose: `yarn test:unit` runs it directly.
 */
export const DELETE_PREVIEW_WAIT_MS = 5_000;

/** `pending`'s value, or `fallback` when it fails or does not settle within `ms`. */
export function settleWithin<T>(pending: Promise<T>, ms: number, fallback: T): Promise<T> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(fallback), ms);
    pending.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      () => {
        clearTimeout(timer);
        resolve(fallback);
      },
    );
  });
}
