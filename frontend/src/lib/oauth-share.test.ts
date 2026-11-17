/**
 * Unit tests for the OAuth sharing decisions - `yarn test:unit`.
 *
 * Sharing an OAuth integration shares its connected account: the
 * Configuration card's "Don't share my connected account" switch starts OFF,
 * except on an integration whose owner already opted out, and every save of
 * an integration that ends up shared over OAuth carries the choice, so an
 * unrelated edit never flips it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canAuthorizeIntegration,
  credentialShareChoice,
  isConnectedAccountWithheld,
  isSharedOauthIntegration,
  isSharedTokenUnacknowledged,
  lendsConnectedAccount,
  type OauthShareFields,
} from "./oauth-share.ts";

const integration = (overrides: Partial<OauthShareFields> = {}): OauthShareFields => ({
  auth_type: "oauth",
  is_company_managed: false,
  shared_group_ids: null,
  shared_user_ids: null,
  oauth_share_acknowledged: false,
  has_access_token: true,
  ...overrides,
});

test("every kind of share counts: company-wide, a group, a person", () => {
  assert.equal(isSharedOauthIntegration(integration()), false);
  assert.equal(isSharedOauthIntegration(integration({ is_company_managed: true })), true);
  assert.equal(isSharedOauthIntegration(integration({ shared_group_ids: ["g"] })), true);
  assert.equal(isSharedOauthIntegration(integration({ shared_user_ids: ["u"] })), true);
  assert.equal(isSharedOauthIntegration(integration({ shared_group_ids: [], shared_user_ids: [] })), false);
  assert.equal(isSharedOauthIntegration(integration({ auth_type: "api_key", is_company_managed: true })), false);
});

test("the switch starts off: a personal integration shares its account once shared", () => {
  assert.equal(isConnectedAccountWithheld(integration()), false);
});

test("the switch starts off on a shared integration sharing its account", () => {
  const shared = integration({ shared_group_ids: ["g"], oauth_share_acknowledged: true });
  assert.equal(isConnectedAccountWithheld(shared), false);
});

test("the switch starts on only where the owner already opted out", () => {
  const optedOut = integration({ shared_group_ids: ["g"], has_access_token: false });
  assert.equal(isConnectedAccountWithheld(optedOut), true);
});

test("a shared token nobody consented for starts off: saving shares it", () => {
  const legacy = integration({ is_company_managed: true });
  assert.equal(isConnectedAccountWithheld(legacy), false);
  assert.equal(isSharedTokenUnacknowledged(legacy), true);
  assert.equal(isSharedTokenUnacknowledged(integration({ is_company_managed: true, oauth_share_acknowledged: true })), false);
  assert.equal(isSharedTokenUnacknowledged(integration()), false);
});

test("a save that shares over OAuth sends the choice: true by default, false for the opt-out", () => {
  assert.equal(credentialShareChoice({ authType: "oauth", willBeShared: true, withholdConnectedAccount: false }), true);
  assert.equal(credentialShareChoice({ authType: "oauth", willBeShared: true, withholdConnectedAccount: true }), false);
});

test("someone who may not consent never asks to share an account not yet shared", () => {
  const base = { authType: "oauth", willBeShared: true, withholdConnectedAccount: false };
  assert.equal(credentialShareChoice({ ...base, mayConsent: false, acknowledged: false }), undefined);
  // An account already shared keeps round-tripping, and the opt-out stays theirs.
  assert.equal(credentialShareChoice({ ...base, mayConsent: false, acknowledged: true }), true);
  assert.equal(
    credentialShareChoice({ ...base, withholdConnectedAccount: true, mayConsent: false, acknowledged: false }),
    false,
  );
  // The account holder asks as before.
  assert.equal(credentialShareChoice({ ...base, mayConsent: true, acknowledged: false }), true);
});

test("a save that does not share, or not over OAuth, sends no choice", () => {
  assert.equal(credentialShareChoice({ authType: "oauth", willBeShared: false, withholdConnectedAccount: true }), undefined);
  assert.equal(credentialShareChoice({ authType: "api_key", willBeShared: true, withholdConnectedAccount: false }), undefined);
});

test("an opted-out integration round-trips its opt-out on an unrelated save", () => {
  const optedOut = integration({ shared_group_ids: ["g"], has_access_token: false });
  const withhold = isConnectedAccountWithheld(optedOut);
  assert.equal(
    credentialShareChoice({ authType: "oauth", willBeShared: isSharedOauthIntegration(optedOut), withholdConnectedAccount: withhold }),
    false,
  );
});

test("only the owner authorizes a personal integration, only an admin a company-managed one", () => {
  const personal = { is_company_managed: false, user_id: "owner" };
  assert.equal(canAuthorizeIntegration(personal, "owner", false), true);
  // A share (even with the config grant) or the admin role does not let anyone
  // else store their token under the owner's name.
  assert.equal(canAuthorizeIntegration(personal, "colleague", false), false);
  assert.equal(canAuthorizeIntegration(personal, "colleague", true), false);
  assert.equal(canAuthorizeIntegration(personal, undefined, false), false);

  const company = { is_company_managed: true, user_id: "owner" };
  assert.equal(canAuthorizeIntegration(company, "colleague", true), true);
  assert.equal(canAuthorizeIntegration(company, "owner", false), false);
  assert.equal(canAuthorizeIntegration({ is_company_managed: true, user_id: null }, "admin", true), true);
});

test("the served can_authorize answers, whatever the fields say", () => {
  const personal = { is_company_managed: false, user_id: "owner" };
  assert.equal(canAuthorizeIntegration({ ...personal, can_authorize: false }, "owner", true), false);
  assert.equal(canAuthorizeIntegration({ ...personal, can_authorize: true }, "colleague", false), true);
  const company = { is_company_managed: true, user_id: null };
  assert.equal(canAuthorizeIntegration({ ...company, can_authorize: false }, "admin", true), false);
});

test("an opt-out is kept: the switch stays on and the owner's own token is no unconsented leftover", () => {
  const optedOut = integration({ shared_group_ids: ["g"], credential_mode: "per_user", has_access_token: true });

  assert.equal(isConnectedAccountWithheld(optedOut), true);
  assert.equal(isSharedTokenUnacknowledged(optedOut), false);
  assert.equal(
    credentialShareChoice({ authType: "oauth", willBeShared: true, withholdConnectedAccount: isConnectedAccountWithheld(optedOut) }),
    false,
  );
});

test("authorizing lends the account only when the integration shares it", () => {
  assert.equal(lendsConnectedAccount(integration({ shared_group_ids: ["g"] })), true);
  assert.equal(lendsConnectedAccount(integration({ shared_group_ids: ["g"], credential_mode: "per_user" })), false);
  assert.equal(lendsConnectedAccount(integration()), false);
});
