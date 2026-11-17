import { describe, expect, it } from "vitest";
import { missingRequiredSsoFields, type SsoRequiredInput } from "./sso-required-fields";

const base: SsoRequiredInput = {
  oidcEnabled: false,
  oidcDisplayName: "Microsoft",
  oidcDiscoveryUrl: "",
  discoveryOptional: false,
  oidcClientId: "",
  oidcClientSecret: "",
  oidcHasClientId: false,
  oidcHasClientSecret: false,
  samlEnabled: false,
  samlDisplayName: "Corporate SSO",
  samlIdpMetadataUrl: "",
};

describe("missingRequiredSsoFields", () => {
  it("asks nothing while both providers are off", () => {
    expect(missingRequiredSsoFields(base)).toEqual([]);
  });

  it("refuses OIDC on with no discovery URL, client ID or secret, in the form's order", () => {
    expect(missingRequiredSsoFields({ ...base, oidcEnabled: true })).toEqual(["oidc-discovery", "oidc-client-id", "oidc-client-secret"]);
  });

  it("refuses SAML on with an empty metadata URL and a blank display name", () => {
    expect(missingRequiredSsoFields({ ...base, samlEnabled: true, samlDisplayName: "  " })).toEqual(["saml-name", "saml-metadata"]);
  });

  it("refuses both providers on and empty, OIDC fields first", () => {
    expect(missingRequiredSsoFields({ ...base, oidcEnabled: true, samlEnabled: true })).toEqual([
      "oidc-discovery",
      "oidc-client-id",
      "oidc-client-secret",
      "saml-metadata",
    ]);
  });

  it("counts a stored client ID and secret as filled", () => {
    expect(
      missingRequiredSsoFields({ ...base, oidcEnabled: true, oidcDiscoveryUrl: "https://idp/.well-known/openid-configuration", oidcHasClientId: true, oidcHasClientSecret: true }),
    ).toEqual([]);
  });

  it("lets the three manual endpoints stand in for the discovery URL", () => {
    expect(
      missingRequiredSsoFields({ ...base, oidcEnabled: true, discoveryOptional: true, oidcClientId: "app", oidcClientSecret: "secret" }),
    ).toEqual([]);
  });

  it("accepts a complete SAML provider", () => {
    expect(missingRequiredSsoFields({ ...base, samlEnabled: true, samlIdpMetadataUrl: "https://idp/metadata.xml" })).toEqual([]);
  });
});
