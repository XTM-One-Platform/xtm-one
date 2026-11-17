// SPDX-License-Identifier: Apache-2.0

/**
 * The platform's Terms of Use consent gate: pending while the
 * platform requires the terms and has not accepted the version in force.
 * `TermsGate` renders the consent screen instead of the app on this answer,
 * and what must not act behind that screen reads the same one (the waiting
 * arcade).
 *
 * `accepted` is forced true server-side whenever the gate does not apply
 * (configuration off, Community Edition). A cached `/auth/me` from a backend
 * that predates the field is not pending.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */
export function termsOfUsePending(terms: { required: boolean; accepted: boolean } | null | undefined): boolean {
  return !!terms?.required && !terms.accepted;
}
