// SPDX-License-Identifier: Apache-2.0

/**
 * Sharing an OAuth integration shares its connected account.
 *
 * Once an OAuth integration is shared (a group, a person, company-wide),
 * everyone it is shared with acts using the account it is connected with, and
 * the backend records the owner's consent (`oauth_share_acknowledged`). The
 * one choice left is the opt-out, off by default: share the integration
 * without the owner's account (`keep_oauth_credentials_on_share: false`). The
 * backend stores it (`credential_mode: "per_user"`): the owner's
 * account stays theirs - authorizing again does not share it - and everyone
 * else it is shared with authorizes with their own account.
 *
 * Dependency-free on purpose: the Configuration card, the integration page
 * and the dashboard read these, and `yarn test:unit` runs them directly.
 */

/** The integration fields the sharing decisions read. */
export interface OauthShareFields {
  auth_type: string;
  is_company_managed: boolean;
  shared_group_ids?: string[] | null;
  shared_user_ids?: string[] | null;
  oauth_share_acknowledged: boolean;
  has_access_token: boolean;
  credential_mode?: string | null;
}

type SharingFields = Pick<
  OauthShareFields,
  "auth_type" | "is_company_managed" | "shared_group_ids" | "shared_user_ids"
>;

/**
 * True when an integration is shared without its owner's account (the
 * opt-out): each person it is shared with authorizes their own.
 */
export function isPerUserIntegration(integ: { credential_mode?: string | null }): boolean {
  return integ.credential_mode === "per_user";
}

/**
 * True when an OAuth integration is shared (company-managed, group or
 * per-user grant): everyone it is shared with uses its connected account.
 */
export function isSharedOauthIntegration(integ: SharingFields): boolean {
  return (
    integ.auth_type === "oauth"
    && (integ.is_company_managed || !!integ.shared_group_ids?.length || !!integ.shared_user_ids?.length)
  );
}

/**
 * True when a shared OAuth integration lends its connected account to everyone
 * it is shared with - authorizing it then connects them all with the
 * authorizer's account. Not once its owner opted out.
 */
export function lendsConnectedAccount(integ: SharingFields & { credential_mode?: string | null }): boolean {
  return isSharedOauthIntegration(integ) && !isPerUserIntegration(integ);
}

/**
 * Who may start an integration's OAuth authorization - the backend's rule
 * (`start_oauth`): the stored token becomes the owner's, so a personal
 * integration is authorized by its owner only and a company-managed one by an
 * admin. Managing an integration through a share does not grant it.
 *
 * The server's `can_authorize` answers when the response carries it: it is
 * `helpers.can_authorize_integration()`, the gate `start_oauth` enforces.
 * Without it, the same rule on the fields at hand.
 */
export function canAuthorizeIntegration(
  integ: { is_company_managed: boolean; user_id?: string | null; can_authorize?: boolean },
  userId: string | null | undefined,
  isAdmin: boolean,
): boolean {
  if (typeof integ.can_authorize === "boolean") return integ.can_authorize;
  if (integ.is_company_managed) return isAdmin;
  return !!userId && integ.user_id === userId;
}

/**
 * True when a shared OAuth integration is shared WITHOUT its owner's account:
 * the owner opted out (stored as `credential_mode: "per_user"`, the
 * owner's own token kept), or - before that was stored - no consent is
 * recorded and no token is stored. This is the initial state of the "Don't
 * share my connected account" switch.
 */
export function isConnectedAccountWithheld(integ: OauthShareFields): boolean {
  return (
    isSharedOauthIntegration(integ)
    && (isPerUserIntegration(integ) || (!integ.oauth_share_acknowledged && !integ.has_access_token))
  );
}

/**
 * True when a shared OAuth integration holds a token nobody consented to
 * share - left over from before sharing recorded the consent. Other members
 * cannot pick it in a chat or test it until the integration's configuration is
 * saved, which records the consent (the switch starts off for it); what it is
 * bound to uses it all the same.
 */
export function isSharedTokenUnacknowledged(integ: OauthShareFields): boolean {
  return (
    isSharedOauthIntegration(integ)
    && !isPerUserIntegration(integ)
    && !integ.oauth_share_acknowledged
    && integ.has_access_token
  );
}

/**
 * The `keep_oauth_credentials_on_share` value a save sends, or `undefined` to
 * send none. An integration that ends up shared over OAuth always carries the
 * choice the form shows, so an unrelated edit keeps what the owner chose: an
 * opted-out integration stays opted out, any other one shares its account.
 *
 * Sharing the connected account is its holder's consent (the owner, the
 * token's recorded owner, an admin). Anyone else never asks for it on an
 * account not shared yet - the backend would refuse - so their edit saves
 * with the consent left as it is (`mayConsent` false, `acknowledged` false).
 */
export function credentialShareChoice(input: {
  authType: string;
  willBeShared: boolean;
  withholdConnectedAccount: boolean;
  mayConsent?: boolean;
  acknowledged?: boolean;
}): boolean | undefined {
  if (input.authType !== "oauth" || !input.willBeShared) return undefined;
  if (input.withholdConnectedAccount) return false;
  if (input.mayConsent === false && !input.acknowledged) return undefined;
  return true;
}
