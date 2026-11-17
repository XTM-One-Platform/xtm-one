// SPDX-License-Identifier: Apache-2.0

/**
 * The required fields of the Authentication form (Settings > Access and
 * security > Authentication): a provider switched on must be able to sign
 * someone in, so its required fields are checked on Save and the Save is not
 * sent while one is empty. A stored client ID or secret counts as filled (the
 * form leaves it empty to keep it); three manual endpoint URLs stand in for
 * the Discovery URL.
 */
export type SsoRequiredField =
  | "oidc-name"
  | "oidc-discovery"
  | "oidc-client-id"
  | "oidc-client-secret"
  | "saml-name"
  | "saml-metadata";

export interface SsoRequiredInput {
  oidcEnabled: boolean;
  oidcDisplayName: string;
  oidcDiscoveryUrl: string;
  discoveryOptional: boolean;
  oidcClientId: string;
  oidcClientSecret: string;
  oidcHasClientId: boolean;
  oidcHasClientSecret: boolean;
  samlEnabled: boolean;
  samlDisplayName: string;
  samlIdpMetadataUrl: string;
}

/** The empty required fields, in the form's order (the first one gets the focus). */
export function missingRequiredSsoFields(form: SsoRequiredInput): SsoRequiredField[] {
  const missing: SsoRequiredField[] = [];
  const empty = (value: string) => !value.trim();
  if (form.oidcEnabled) {
    if (empty(form.oidcDisplayName)) missing.push("oidc-name");
    if (!form.discoveryOptional && empty(form.oidcDiscoveryUrl)) missing.push("oidc-discovery");
    if (!form.oidcHasClientId && empty(form.oidcClientId)) missing.push("oidc-client-id");
    if (!form.oidcHasClientSecret && empty(form.oidcClientSecret)) missing.push("oidc-client-secret");
  }
  if (form.samlEnabled) {
    if (empty(form.samlDisplayName)) missing.push("saml-name");
    if (empty(form.samlIdpMetadataUrl)) missing.push("saml-metadata");
  }
  return missing;
}
