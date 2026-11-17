// SPDX-License-Identifier: Apache-2.0

/**
 * Which toasts the provider (`toast.tsx`) shows once one more is shown.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/**
 * `showing` with `next` added last. A toast carrying the `tag` of one still on
 * screen takes its place (the Notifications API's `tag`): the same message
 * asked for again is shown once, fresh, never stacked.
 */
export function showToast<T extends { tag?: string }>(showing: readonly T[], next: T): T[] {
  const kept = next.tag === undefined ? showing : showing.filter((toast) => toast.tag !== next.tag);
  return [...kept, next];
}
