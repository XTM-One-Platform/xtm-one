// SPDX-License-Identifier: Apache-2.0

import { useId, useState } from "react";
import { IconButton, Spinner } from "@filigran/design-system";
import {
  AtSign,
  Key,
  Check,
  User as UserIcon,
  Users,
  Globe,
  CheckCircle2,
  XCircle,
  Pencil,
  Shield,
  Lock,
  Info,
  Copy,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SecretField } from "@/components/ui/secret-field";
import {
  SettingsField,
  SettingsFieldGrid,
  SettingsGroupTitle,
  SettingsSaveBar,
  SettingsSwitchField,
  focusFirstInvalidField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { useSSOConfig, useTestOidcDiscovery, useUpdateSSOConfig } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { missingRequiredSsoFields, type SsoRequiredField } from "./sso-required-fields";

// ---------------------------------------------------------------------------
// Copyable read-only field (used in SSO config guides)
// ---------------------------------------------------------------------------
function CopyableField({ label, value, copied, onCopy }: { label: string; value: string; copied: string; onCopy: (text: string, label: string) => void }) {
  // The copy button's accessible name names the field it copies, so this
  // sub-component needs the formatter of its own.
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center justify-between rounded-sm bg-elevation-default p-2.5">
      <div className="min-w-0 flex-1">
        <p className="content-compact text-default-secondary mb-0.5">{label}</p>
        <p className="content-compact font-mono-plex text-accent truncate">{value}</p>
      </div>
      <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Copy {field}", { field: label })} icon={copied === label ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />} className="h-7 w-7 shrink-0 ml-2" onClick={() => onCopy(value, label)} />
    </div>
  );
}

/** A stored list (domains joined by commas, scopes by spaces) as the items a TagInput edits. */
function splitList(text: string, separator: RegExp): string[] {
  return text.split(separator).map((item) => item.trim()).filter(Boolean);
}

type SSOConfig = NonNullable<ReturnType<typeof useSSOConfig>["data"]>;

/** The form as Configure opens it on a stored config (secrets empty: kept unless typed), in the key order of the form's payload. */
function payloadOf(c: SSOConfig) {
  return {
    oidc_enabled: c.oidc_enabled,
    oidc_display_name: c.oidc_display_name,
    oidc_client_id: "",
    oidc_client_secret: "",
    oidc_discovery_url: c.oidc_discovery_url,
    oidc_authorization_url: c.oidc_authorization_url,
    oidc_token_url: c.oidc_token_url,
    oidc_userinfo_url: c.oidc_userinfo_url,
    oidc_scopes: c.oidc_scopes,
    saml_enabled: c.saml_enabled,
    saml_display_name: c.saml_display_name,
    saml_idp_metadata_url: c.saml_idp_metadata_url,
    saml_sp_entity_id: c.saml_sp_entity_id,
    saml_requested_authn_context: c.saml_requested_authn_context,
    saml_groups_mapping_enabled: c.saml_groups_mapping_enabled,
    saml_groups_auto_create: c.saml_groups_auto_create,
    oidc_auto_admin_new_users: c.oidc_auto_admin_new_users,
    saml_auto_admin_new_users: c.saml_auto_admin_new_users,
    oidc_auto_create_users: c.oidc_auto_create_users,
    saml_auto_create_users: c.saml_auto_create_users,
    local_auth_enabled: c.local_auth_enabled,
    registration_enabled: c.registration_enabled,
    allowed_domains: c.allowed_domains,
  };
}

// ---------------------------------------------------------------------------
// SSO Configuration (admin-only section)
// ---------------------------------------------------------------------------
export function SSOConfigSection() {
  const { t_i18n } = useFormatter();
  const { data: ssoConfig } = useSSOConfig();
  const allowedDomainList = ssoConfig?.allowed_domain_list ?? [];
  const updateSSO = useUpdateSSOConfig();
  const testDiscovery = useTestOidcDiscovery();
  const fid = useId();
  const [editing, setEditing] = useState(false);
  const [openedAs, setOpenedAs] = useState("");
  const [copied, setCopied] = useState("");

  // Form state
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [oidcDisplayName, setOidcDisplayName] = useState("Microsoft");
  const [oidcClientId, setOidcClientId] = useState("");
  const [oidcClientSecret, setOidcClientSecret] = useState("");
  const [oidcDiscoveryUrl, setOidcDiscoveryUrl] = useState("");
  const [oidcAuthUrl, setOidcAuthUrl] = useState("");
  const [oidcTokenUrl, setOidcTokenUrl] = useState("");
  const [oidcUserinfoUrl, setOidcUserinfoUrl] = useState("");
  const [oidcScopes, setOidcScopes] = useState("openid email profile");
  const [samlEnabled, setSamlEnabled] = useState(false);
  const [samlDisplayName, setSamlDisplayName] = useState("Corporate SSO");
  const [samlIdpMetadataUrl, setSamlIdpMetadataUrl] = useState("");
  const [samlSpEntityId, setSamlSpEntityId] = useState("");
  const [samlRequestedAuthnContext, setSamlRequestedAuthnContext] = useState(false);
  const [samlGroupsMappingEnabled, setSamlGroupsMappingEnabled] = useState(true);
  const [samlGroupsAutoCreate, setSamlGroupsAutoCreate] = useState(true);
  const [oidcAutoAdminNewUsers, setOidcAutoAdminNewUsers] = useState(false);
  const [samlAutoAdminNewUsers, setSamlAutoAdminNewUsers] = useState(false);
  const [oidcAutoCreateUsers, setOidcAutoCreateUsers] = useState(true);
  const [samlAutoCreateUsers, setSamlAutoCreateUsers] = useState(true);
  const [localAuthEnabled, setLocalAuthEnabled] = useState(true);
  const [regEnabled, setRegEnabled] = useState(false);
  const [allowedDomains, setAllowedDomains] = useState("");
  const [saveAttempted, setSaveAttempted] = useState(false);

  const startEditing = () => {
    setSaveAttempted(false);
    if (ssoConfig) {
      setOidcEnabled(ssoConfig.oidc_enabled);
      setOidcDisplayName(ssoConfig.oidc_display_name);
      setOidcClientId("");
      setOidcClientSecret("");
      setOidcDiscoveryUrl(ssoConfig.oidc_discovery_url);
      setOidcAuthUrl(ssoConfig.oidc_authorization_url);
      setOidcTokenUrl(ssoConfig.oidc_token_url);
      setOidcUserinfoUrl(ssoConfig.oidc_userinfo_url);
      setOidcScopes(ssoConfig.oidc_scopes);
      setSamlEnabled(ssoConfig.saml_enabled);
      setSamlDisplayName(ssoConfig.saml_display_name);
      setSamlIdpMetadataUrl(ssoConfig.saml_idp_metadata_url);
      setSamlSpEntityId(ssoConfig.saml_sp_entity_id);
      setSamlRequestedAuthnContext(ssoConfig.saml_requested_authn_context);
      setSamlGroupsMappingEnabled(ssoConfig.saml_groups_mapping_enabled);
      setSamlGroupsAutoCreate(ssoConfig.saml_groups_auto_create);
      setOidcAutoAdminNewUsers(ssoConfig.oidc_auto_admin_new_users);
      setSamlAutoAdminNewUsers(ssoConfig.saml_auto_admin_new_users);
      setOidcAutoCreateUsers(ssoConfig.oidc_auto_create_users);
      setSamlAutoCreateUsers(ssoConfig.saml_auto_create_users);
      setLocalAuthEnabled(ssoConfig.local_auth_enabled);
      setRegEnabled(ssoConfig.registration_enabled);
      setAllowedDomains(ssoConfig.allowed_domains);
      setOpenedAs(JSON.stringify(payloadOf(ssoConfig)));
    }
    setEditing(true);
  };

  const formPayload = () => ({
        oidc_enabled: oidcEnabled,
        oidc_display_name: oidcDisplayName,
        oidc_client_id: oidcClientId,
        oidc_client_secret: oidcClientSecret,
        oidc_discovery_url: oidcDiscoveryUrl,
        oidc_authorization_url: oidcAuthUrl,
        oidc_token_url: oidcTokenUrl,
        oidc_userinfo_url: oidcUserinfoUrl,
        oidc_scopes: oidcScopes,
        saml_enabled: samlEnabled,
        saml_display_name: samlDisplayName,
        saml_idp_metadata_url: samlIdpMetadataUrl,
        saml_sp_entity_id: samlSpEntityId,
        saml_requested_authn_context: samlRequestedAuthnContext,
        saml_groups_mapping_enabled: samlGroupsMappingEnabled,
        saml_groups_auto_create: samlGroupsAutoCreate,
        oidc_auto_admin_new_users: oidcAutoAdminNewUsers,
        saml_auto_admin_new_users: samlAutoAdminNewUsers,
        oidc_auto_create_users: oidcAutoCreateUsers,
        saml_auto_create_users: samlAutoCreateUsers,
        local_auth_enabled: localAuthEnabled,
        registration_enabled: regEnabled,
        allowed_domains: allowedDomains,
  });
  // Unsaved changes: the form as it stands against the form as Configure opened it.
  const dirty = editing && JSON.stringify(formPayload()) !== openedAs;
  // The three manual endpoints stand in for the Discovery URL.
  const discoveryOptional = Boolean(oidcAuthUrl.trim() && oidcTokenUrl.trim() && oidcUserinfoUrl.trim());

  // A provider switched on must be usable: its required fields are checked on
  // Save, in the form's order, and Save is not sent while one is empty.
  const missing = missingRequiredSsoFields({
    oidcEnabled,
    oidcDisplayName,
    oidcDiscoveryUrl,
    discoveryOptional,
    oidcClientId,
    oidcClientSecret,
    oidcHasClientId: Boolean(ssoConfig?.oidc_has_client_id),
    oidcHasClientSecret: Boolean(ssoConfig?.oidc_has_client_secret),
    samlEnabled,
    samlDisplayName,
    samlIdpMetadataUrl,
  });
  const fieldError = (field: SsoRequiredField) => (saveAttempted && missing.includes(field) ? t_i18n("This field is required.") : undefined);
  const ssoFieldId = (field: SsoRequiredField) => `${fid}-${field}`;

  const handleSave = () => {
    if (missing.length > 0) {
      setSaveAttempted(true);
      focusFirstInvalidField(missing.map(ssoFieldId));
      return;
    }
    updateSSO.mutate(formPayload(), { onSuccess: () => setEditing(false) });
  };

  const copyToClipboard = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(""), COPY_FEEDBACK_DELAY);
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Lock className="h-4 w-4 text-accent" />
              {t_i18n("Single Sign-On (SSO)")}
            </CardTitle>
            <CardDescription className="mt-1">
              {t_i18n("Configure OpenID Connect (OIDC) and SAML 2.0 for enterprise authentication. Users are auto-created on first login and linked by email to existing accounts.")}
            </CardDescription>
          </div>
          {!editing && (
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing}>
              <Pencil className="h-3 w-3" />
              {t_i18n("Configure")}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* ─── Status Overview ─── */}
        {!editing && ssoConfig && (
          <div className="space-y-3">
            {/* Local auth */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-elevation-hover">
                  <Key className="h-4 w-4 text-default-secondary" />
                </div>
                <div>
                  <p className="content-base font-medium">{t_i18n("Local Authentication")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Email & password login")}</p>
                </div>
              </div>
              {ssoConfig.local_auth_enabled ? (
                <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Enabled")}</Badge>
              ) : (
                <Badge variant="destructive" className="content-caption"><XCircle className="h-3 w-3 mr-1" />{t_i18n("Disabled")}</Badge>
              )}
            </div>
            {/* Registration */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-elevation-hover">
                  <UserIcon className="h-4 w-4 text-default-secondary" />
                </div>
                <div>
                  <p className="content-base font-medium">{t_i18n("Local Registration")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Let new users create accounts with email & password")}</p>
                </div>
              </div>
              {ssoConfig.registration_enabled ? (
                <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Enabled")}</Badge>
              ) : (
                <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
              )}
            </div>
            {/* Allowed email domains */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-elevation-hover">
                  <AtSign className="h-4 w-4 text-default-secondary" />
                </div>
                <div>
                  <p className="content-base font-medium">{t_i18n("Allowed email domains")}</p>
                  <p className="content-compact text-default-secondary">
                    {allowedDomainList.length > 0
                      ? t_i18n("New accounts (local registration and SSO) restricted to: {domains}", { domains: allowedDomainList.join(", ") })
                      : t_i18n("New accounts (local registration and SSO) from any email domain")}
                  </p>
                </div>
              </div>
              {allowedDomainList.length > 0 ? (
                <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Restricted")}</Badge>
              ) : (
                <Badge variant="outline" className="content-caption">{t_i18n("Any domain")}</Badge>
              )}
            </div>
            {/* OIDC */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-accent/10">
                  <Globe className="h-4 w-4 text-accent" />
                </div>
                <div>
                  <p className="content-base font-medium">{t_i18n("OpenID Connect")}</p>
                  <p className="content-compact text-default-secondary">{ssoConfig.oidc_display_name} · {t_i18n("Authorization Code flow")}</p>
                </div>
              </div>
              {ssoConfig.oidc_enabled ? (
                <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Enabled")}</Badge>
              ) : (
                <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
              )}
            </div>
            {/* SAML */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-accent/10">
                  <Shield className="h-4 w-4 text-accent" />
                </div>
                <div>
                  <p className="content-base font-medium">SAML 2.0</p>
                  <p className="content-compact text-default-secondary">{ssoConfig.saml_display_name} · {t_i18n("SP-initiated flow")}</p>
                </div>
              </div>
              {ssoConfig.saml_enabled ? (
                <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Enabled")}</Badge>
              ) : (
                <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
              )}
            </div>
            {/* SAML Group Mapping */}
            {ssoConfig.saml_enabled && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-accent/10">
                    <Users className="h-4 w-4 text-accent" />
                  </div>
                  <div>
                    <p className="content-base font-medium">{t_i18n("SAML Group Mapping")}</p>
                    <p className="content-compact text-default-secondary">
                      {ssoConfig.saml_groups_mapping_enabled
                        ? t_i18n("Auto-sync groups from IdP") + (ssoConfig.saml_groups_auto_create ? " · " + t_i18n("Auto-create enabled") : "")
                        : t_i18n("Group synchronization disabled")}
                    </p>
                  </div>
                </div>
                {ssoConfig.saml_groups_mapping_enabled ? (
                  <Badge variant="success" className="content-caption"><CheckCircle2 className="h-3 w-3 mr-1" />{t_i18n("Enabled")}</Badge>
                ) : (
                  <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
                )}
              </div>
            )}
          </div>
        )}

        {/* ─── Editing Form: every field on the settings form kit ─── */}
        {editing && (
          <div className="space-y-8">
            {/* === General Auth === */}
            <div className="space-y-4">
              <SettingsGroupTitle icon={UserIcon}>
                {t_i18n("Local Authentication")}
              </SettingsGroupTitle>
              <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
                <SettingsSwitchField
                  id={`${fid}-local-auth`}
                  label={t_i18n("Allow email & password login")}
                  helper={t_i18n("Disable to force all users to sign in via SSO")}
                  info={t_i18n("On: people can sign in with an email and a password. Off: everyone signs in through OIDC or SAML, so keep one SSO provider working before you turn it off.")}
                  checked={localAuthEnabled}
                  onCheckedChange={setLocalAuthEnabled}
                />
                <SettingsSwitchField
                  id={`${fid}-registration`}
                  label={t_i18n("Allow local registration")}
                  helper={t_i18n("Let new users create accounts with email & password")}
                  info={t_i18n("On: anyone with an allowed email domain can create an account from the sign-in page. Off: accounts come from SSO or from an administrator.")}
                  checked={regEnabled}
                  disabled={!localAuthEnabled}
                  onCheckedChange={setRegEnabled}
                />
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* === New accounts: the domains gate SSO too, so they stay editable with local auth off === */}
            <div className="space-y-4">
              <SettingsGroupTitle icon={AtSign}>
                {t_i18n("New accounts")}
              </SettingsGroupTitle>
              <SettingsField
                id={`${fid}-domains`}
                label={t_i18n("Allowed email domains")}
                helper={t_i18n("Empty: new accounts from any email domain.")}
                info={t_i18n("Local registration and SSO create an account only for an email address at one of these domains. Existing accounts keep signing in whatever their domain.")}
              >
                <TagInput
                  aria-labelledby={settingsFieldLabelId(`${fid}-domains`)}
                  aria-describedby={settingsFieldHintId(`${fid}-domains`)}
                  value={splitList(allowedDomains, /[\s,]+/)}
                  onChange={(domains) => setAllowedDomains(domains.join(", "))}
                  placeholder={t_i18n("Add a domain, e.g. example.com")}
                  emptyLabel={t_i18n("Any domain")}
                  suggestFromPlatformTags={false}
                />
              </SettingsField>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* === OIDC === */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <SettingsGroupTitle icon={Globe}>
                  {t_i18n("OpenID Connect (OIDC)")}
                </SettingsGroupTitle>
                <Switch aria-label={t_i18n("OpenID Connect (OIDC)")} checked={oidcEnabled} onCheckedChange={setOidcEnabled} />
              </div>

              {oidcEnabled && (
                <div className="space-y-4 rounded-sm bg-accent/[0.02] p-4">
                  <SettingsFieldGrid>
                    <SettingsField
                      id={`${fid}-oidc-name`}
                      label={t_i18n("Display Name")}
                      helper={t_i18n("The label of the sign-in button, e.g. Microsoft.")}
                      info={t_i18n("Shown on the sign-in button and in the read view of this page, e.g. Microsoft or Okta. It is only a label and changes nothing in the sign-in flow.")}
                      required
                      error={fieldError("oidc-name")}
                    >
                      <Input id={`${fid}-oidc-name`} aria-labelledby={settingsFieldLabelId(`${fid}-oidc-name`)} aria-describedby={settingsFieldHintId(`${fid}-oidc-name`)} aria-required value={oidcDisplayName} onChange={(e) => setOidcDisplayName(e.target.value)} placeholder="Microsoft" />
                    </SettingsField>
                    <SettingsField
                      id={`${fid}-oidc-discovery`}
                      label={t_i18n("Discovery URL")}
                      helper={t_i18n("Use the provider's /.well-known/openid-configuration URL, not the issuer URL on its own.")}
                      info={t_i18n("The OpenID configuration document of your identity provider. In Entra ID: App registrations, then Endpoints. Empty: set the three endpoint URLs under Advanced instead.")}
                      required={!discoveryOptional}
                      error={fieldError("oidc-discovery")}
                    >
                      <Input id={`${fid}-oidc-discovery`} aria-labelledby={settingsFieldLabelId(`${fid}-oidc-discovery`)} aria-describedby={settingsFieldHintId(`${fid}-oidc-discovery`)} aria-required={!discoveryOptional} value={oidcDiscoveryUrl} onChange={(e) => { setOidcDiscoveryUrl(e.target.value); testDiscovery.reset(); }} placeholder="https://login.microsoftonline.com/{tenant}/.well-known/openid-configuration" />
                    </SettingsField>
                  </SettingsFieldGrid>
                  <div className="flex items-start justify-end gap-2">
                    {testDiscovery.data && (
                      <p role="status" className={`content-compact ${testDiscovery.data.ok ? "text-feedback-success-primary" : "text-feedback-error-primary"}`}>
                        {testDiscovery.data.ok ? t_i18n("The Discovery URL is usable.") : testDiscovery.data.detail}
                      </p>
                    )}
                    <Button variant="outline" onClick={() => testDiscovery.mutate(oidcDiscoveryUrl)} disabled={!oidcDiscoveryUrl.trim() || testDiscovery.isPending} aria-busy={testDiscovery.isPending || undefined}>
                      {testDiscovery.isPending && <Spinner size="sm" tone="inherit" />}
                      {t_i18n("Test connection")}
                    </Button>
                  </div>
                  <SettingsFieldGrid>
                    <SettingsField
                      id={`${fid}-oidc-client-id`}
                      label={t_i18n("Client ID")}
                      helper={ssoConfig?.oidc_has_client_id ? t_i18n("A client ID is stored. Leave empty to keep it.") : t_i18n("The application (client) ID your identity provider gave this app.")}
                      info={t_i18n("In Entra ID: App registrations, your app, Overview, Application (client) ID. In Okta: the General tab of the app. Stored encrypted and never shown again.")}
                      required={!ssoConfig?.oidc_has_client_id}
                      error={fieldError("oidc-client-id")}
                    >
                      <SecretField
                        id={`${fid}-oidc-client-id`}
                        aria-label={t_i18n("Client ID")}
                        aria-describedby={settingsFieldHintId(`${fid}-oidc-client-id`)}
                        aria-required={!ssoConfig?.oidc_has_client_id || undefined}
                        value={oidcClientId}
                        onChange={setOidcClientId}
                        hasStoredValue={ssoConfig?.oidc_has_client_id}
                        allowVault={false}
                        placeholder={ssoConfig?.oidc_has_client_id ? t_i18n("Leave empty to keep existing") : t_i18n("Paste client ID...")}
                      />
                    </SettingsField>
                    <SettingsField
                      id={`${fid}-oidc-client-secret`}
                      label={t_i18n("Client Secret")}
                      helper={ssoConfig?.oidc_has_client_secret ? t_i18n("A client secret is stored. Leave empty to keep it.") : t_i18n("A client secret of the same app, or a vault reference.")}
                      info={t_i18n("In Entra ID: App registrations, your app, Certificates & secrets. Stored encrypted and never shown again. Replace it here before it expires at the provider.")}
                      required={!ssoConfig?.oidc_has_client_secret}
                      error={fieldError("oidc-client-secret")}
                    >
                      <SecretField
                        id={`${fid}-oidc-client-secret`}
                        aria-label={t_i18n("Client Secret")}
                        aria-describedby={settingsFieldHintId(`${fid}-oidc-client-secret`)}
                        aria-required={!ssoConfig?.oidc_has_client_secret || undefined}
                        value={oidcClientSecret}
                        onChange={setOidcClientSecret}
                        hasStoredValue={ssoConfig?.oidc_has_client_secret}
                        stored={ssoConfig?.vault_secrets?.locations?.oidc_client_secret?.[0] ?? null}
                        placeholder={t_i18n("Paste client secret...")}
                      />
                    </SettingsField>
                  </SettingsFieldGrid>
                  <SettingsField
                    id={`${fid}-oidc-scopes`}
                    label={t_i18n("Scopes")}
                    helper={t_i18n("The OIDC scopes asked at sign-in. Keep openid and email.")}
                    info={t_i18n("Press Enter after each scope, e.g. openid, email, profile. The email claim links a sign-in to an account. Add a scope only when your provider needs it for a claim.")}
                  >
                    <TagInput
                      aria-labelledby={settingsFieldLabelId(`${fid}-oidc-scopes`)}
                      aria-describedby={settingsFieldHintId(`${fid}-oidc-scopes`)}
                      value={splitList(oidcScopes, /\s+/)}
                      onChange={(scopes) => setOidcScopes(scopes.join(" "))}
                      placeholder={t_i18n("Add a scope, e.g. profile")}
                      preserveCase
                      suggestFromPlatformTags={false}
                    />
                  </SettingsField>

                  {/* Advanced: manual URLs (if not using discovery) */}
                  <details className="group">
                    <DisclosureSummary>
                      {t_i18n("Advanced: Manual endpoint URLs (if not using discovery)")}
                    </DisclosureSummary>
                    <SettingsFieldGrid className="mt-3">
                      {([
                        ["auth", t_i18n("Authorization URL"), oidcAuthUrl, setOidcAuthUrl],
                        ["token", t_i18n("Token URL"), oidcTokenUrl, setOidcTokenUrl],
                        ["userinfo", t_i18n("UserInfo URL"), oidcUserinfoUrl, setOidcUserinfoUrl],
                      ] as const).map(([key, label, value, setValue]) => (
                        <SettingsField
                          key={key}
                          id={`${fid}-oidc-${key}`}
                          label={label}
                          helper={t_i18n("Only when the provider has no discovery document.")}
                          info={t_i18n("An endpoint from your provider's documentation, used only when the Discovery URL is empty. All three are needed in that case.")}
                        >
                          <Input id={`${fid}-oidc-${key}`} aria-labelledby={settingsFieldLabelId(`${fid}-oidc-${key}`)} aria-describedby={settingsFieldHintId(`${fid}-oidc-${key}`)} value={value} onChange={(e) => setValue(e.target.value)} placeholder="https://..." />
                        </SettingsField>
                      ))}
                    </SettingsFieldGrid>
                  </details>

                  <div className="rounded-sm bg-elevation-default p-3 space-y-1">
                    <p className="content-compact text-default-secondary flex items-center gap-1.5">
                      <Info className="h-3 w-3 shrink-0" />
                      {t_i18n("Set the OIDC Redirect URI in your identity provider to:")}
                    </p>
                    <p className="content-compact font-mono-plex text-accent">{ssoConfig?.saml_acs_url?.replace("/saml/acs", "/oidc/callback") || t_i18n("Loading...")}</p>
                  </div>

                  <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
                    <SettingsSwitchField
                      id={`${fid}-oidc-admin`}
                      label={t_i18n("Automatically admin users")}
                      helper={t_i18n("When enabled, everyone who signs in via OIDC is made an administrator at every sign-in, existing accounts included. Turning it off demotes nobody.")}
                      info={t_i18n("Turn it on only when the identity provider already limits who can sign in to this app to administrators.")}
                      checked={oidcAutoAdminNewUsers}
                      onCheckedChange={setOidcAutoAdminNewUsers}
                    />
                    <SettingsSwitchField
                      id={`${fid}-oidc-create`}
                      label={t_i18n("On-the-fly user creation")}
                      helper={t_i18n("When enabled, an account is created automatically on first login, and only for an email address at one of the allowed email domains when any are set. Disable to only allow pre-provisioned accounts (matched by SSO subject or email) to sign in.")}
                      info={t_i18n("Off: an administrator creates the accounts first, and a sign-in without a matching account is refused.")}
                      checked={oidcAutoCreateUsers}
                      onCheckedChange={setOidcAutoCreateUsers}
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* === SAML === */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <SettingsGroupTitle icon={Shield}>
                  SAML 2.0
                </SettingsGroupTitle>
                <Switch aria-label="SAML 2.0" checked={samlEnabled} onCheckedChange={setSamlEnabled} />
              </div>

              {samlEnabled && (
                <div className="space-y-4 rounded-sm bg-accent/[0.02] p-4">
                  <SettingsFieldGrid>
                    <SettingsField
                      id={`${fid}-saml-name`}
                      label={t_i18n("Display Name")}
                      helper={t_i18n("The label of the sign-in button, e.g. Corporate SSO.")}
                      info={t_i18n("Shown on the sign-in button and in the read view of this page, e.g. Microsoft or Okta. It is only a label and changes nothing in the sign-in flow.")}
                      required
                      error={fieldError("saml-name")}
                    >
                      <Input id={`${fid}-saml-name`} aria-labelledby={settingsFieldLabelId(`${fid}-saml-name`)} aria-describedby={settingsFieldHintId(`${fid}-saml-name`)} aria-required value={samlDisplayName} onChange={(e) => setSamlDisplayName(e.target.value)} placeholder="Corporate SSO" />
                    </SettingsField>
                    <SettingsField
                      id={`${fid}-saml-metadata`}
                      label={t_i18n("IdP Metadata URL")}
                      helper={t_i18n("The federation metadata URL of your identity provider.")}
                      info={t_i18n("In Entra ID: Enterprise applications, your app, Single sign-on, App Federation Metadata URL. The platform reads the IdP certificate and endpoints from it.")}
                      required
                      error={fieldError("saml-metadata")}
                    >
                      <Input id={`${fid}-saml-metadata`} aria-labelledby={settingsFieldLabelId(`${fid}-saml-metadata`)}
                        aria-describedby={settingsFieldHintId(`${fid}-saml-metadata`)}
                        aria-required
                        value={samlIdpMetadataUrl}
                        onChange={(e) => setSamlIdpMetadataUrl(e.target.value)}
                        placeholder="https://login.microsoftonline.com/{tenant}/federationmetadata/2007-06/federationmetadata.xml?appid=..."
                      />
                    </SettingsField>
                  </SettingsFieldGrid>
                  <SettingsField
                    id={`${fid}-saml-entity`}
                    label={t_i18n("SP Entity ID")}
                    helper={t_i18n("Empty: the SP metadata URL is used as the Entity ID.")}
                    info={t_i18n("The identifier of this platform at your identity provider. Set it only when the IdP already knows this app under another identifier: it must match the Identifier (Entity ID) configured there.")}
                  >
                    <Input id={`${fid}-saml-entity`} aria-labelledby={settingsFieldLabelId(`${fid}-saml-entity`)} aria-describedby={settingsFieldHintId(`${fid}-saml-entity`)} value={samlSpEntityId} onChange={(e) => setSamlSpEntityId(e.target.value)} placeholder={ssoConfig?.saml_entity_id || ""} />
                  </SettingsField>

                  <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
                    <SettingsSwitchField
                      id={`${fid}-saml-authn`}
                      label={t_i18n("Requested Authentication Context")}
                      helper={t_i18n("When enabled, the SAML request requires password-based authentication. Disable to allow all methods (X509 certificates, MFA, FIDO2, etc.)")}
                      info={t_i18n("Turn it off when people sign in with MFA, a certificate or a passkey: with it on, the identity provider refuses those sign-ins.")}
                      checked={samlRequestedAuthnContext}
                      onCheckedChange={setSamlRequestedAuthnContext}
                    />
                    <SettingsSwitchField
                      id={`${fid}-saml-admin`}
                      label={t_i18n("Automatically admin users")}
                      helper={t_i18n("When enabled, everyone who signs in via SAML is made an administrator at every sign-in, existing accounts included. Turning it off demotes nobody.")}
                      info={t_i18n("Turn it on only when the identity provider already limits who can sign in to this app to administrators.")}
                      checked={samlAutoAdminNewUsers}
                      onCheckedChange={setSamlAutoAdminNewUsers}
                    />
                    <SettingsSwitchField
                      id={`${fid}-saml-create`}
                      label={t_i18n("On-the-fly user creation")}
                      helper={t_i18n("When enabled, an account is created automatically on first login, and only for an email address at one of the allowed email domains when any are set. Disable to only allow pre-provisioned accounts (matched by SSO subject or email) to sign in.")}
                      info={t_i18n("Off: an administrator creates the accounts first, and a sign-in without a matching account is refused.")}
                      checked={samlAutoCreateUsers}
                      onCheckedChange={setSamlAutoCreateUsers}
                    />
                  </div>

                  {/* ── SAML Group Mapping ── */}
                  <div className="space-y-3">
                    <SettingsGroupTitle icon={Users}>
                      {t_i18n("Group Mapping")}
                    </SettingsGroupTitle>
                    <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
                      <SettingsSwitchField
                        id={`${fid}-saml-groups`}
                        label={t_i18n("Automatic Groups Mapping")}
                        helper={t_i18n("Synchronize user group memberships from SAML group claims on each login. Groups not present in the assertion will be removed.")}
                        info={t_i18n("Reads the groups claim of the assertion. In Entra ID, add a group claim to the app first (step 5 of the guide below).")}
                        checked={samlGroupsMappingEnabled}
                        onCheckedChange={setSamlGroupsMappingEnabled}
                      />
                      <SettingsSwitchField
                        id={`${fid}-saml-groups-create`}
                        label={t_i18n("Auto-create Groups")}
                        helper={t_i18n("Automatically create groups in Copilot when they appear in the SAML assertion but don't exist yet")}
                        info={t_i18n("Off: only groups that already exist here are synced, and the other groups of the assertion are ignored.")}
                        checked={samlGroupsAutoCreate}
                        disabled={!samlGroupsMappingEnabled}
                        onCheckedChange={setSamlGroupsAutoCreate}
                      />
                    </div>
                  </div>

                  {/* ── SAML Guidance Panel ── */}
                  <div className="rounded-sm bg-accent/5 p-4 space-y-4">
                    <div className="flex items-center gap-2">
                      <Info className="h-4 w-4 text-accent shrink-0" />
                      <h5 className="content-base font-medium">{t_i18n("Identity Provider Configuration Guide")}</h5>
                    </div>
                    <p className="content-compact text-default-secondary">
                      {t_i18n("Use the values below when configuring your SAML application in Microsoft Entra ID (Azure AD), Okta, Google Workspace, or any SAML 2.0 IdP.")}
                    </p>

                    <div className="space-y-2">
                      <CopyableField
                        label={t_i18n("Identifier (Entity ID): Required")}
                        value={samlSpEntityId.trim() || ssoConfig?.saml_metadata_url || t_i18n("Loading...")}
                        copied={copied}
                        onCopy={copyToClipboard}
                      />
                      <CopyableField
                        label={t_i18n("Reply URL (Assertion Consumer Service URL): Required")}
                        value={ssoConfig?.saml_acs_url || t_i18n("Loading...")}
                        copied={copied}
                        onCopy={copyToClipboard}
                      />
                      <CopyableField
                        label={t_i18n("SP Metadata URL: Optional (auto-discovery)")}
                        value={ssoConfig?.saml_metadata_url || t_i18n("Loading...")}
                        copied={copied}
                        onCopy={copyToClipboard}
                      />
                    </div>

                    <div className="border-t border-elevation-subtle-soft pt-3 space-y-2">
                      <h6 className="content-compact font-medium text-default-secondary">{t_i18n("Required SAML Claims / Attributes")}</h6>
                      <div className="grid gap-2 content-compact">
                        {([
                          ["emailaddress", true, t_i18n("User's email (used for account matching & creation)")],
                          ["name / displayName", false, t_i18n("User's display name")],
                          ["title / jobTitle", false, t_i18n("User's job title (resolved automatically if provided)")],
                        ] as const).map(([claim, required, meaning]) => (
                          <div key={claim} className="flex items-start gap-2">
                            <Badge variant={required ? "cyan" : "outline"} className="content-caption shrink-0 mt-0.5">{required ? t_i18n("Required") : t_i18n("Optional")}</Badge>
                            <div>
                              <span className="font-mono-plex text-accent">{claim}</span>
                              <span className="text-default-secondary">: {meaning}</span>
                            </div>
                          </div>
                        ))}
                        <div className="flex items-start gap-2">
                          <Badge variant="outline" className="content-caption shrink-0 mt-0.5">{t_i18n("Optional")}</Badge>
                          <div>
                            <span className="font-mono-plex text-accent">groups</span>
                            <span className="text-default-secondary">: {t_i18n("Group memberships for automatic group mapping (Microsoft:")} <span className="font-mono-plex">http://schemas.microsoft.com/ws/2008/06/identity/claims/groups</span>)</span>
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="border-t border-elevation-subtle-soft pt-3 space-y-2">
                      <h6 className="content-compact font-medium text-default-secondary">{t_i18n("Microsoft Entra ID (Azure AD) Quick Setup")}</h6>
                      <ol className="list-decimal list-inside content-compact text-default-secondary space-y-1.5">
                        <li>{t_i18n("Go to Enterprise applications, then New application, then Create your own application.")}</li>
                        <li>{t_i18n("Choose \"Integrate any other application you don't find in the gallery (Non-gallery)\".")}</li>
                        <li>{t_i18n("Under Single sign-on, select SAML.")}</li>
                        <li>{t_i18n("In Basic SAML Configuration:")}
                          <ul className="ml-4 mt-1 list-disc space-y-0.5">
                            <li>{t_i18n("Identifier (Entity ID): paste the Entity ID above.")}</li>
                            <li>{t_i18n("Reply URL (ACS URL): paste the Reply URL above.")}</li>
                          </ul>
                        </li>
                        <li>{t_i18n("Under Attributes & Claims, make sure emailaddress is mapped. Add a jobtitle claim to sync titles. For group mapping, click Add a group claim and select the groups to include (e.g. \"All groups\" with sAMAccountName as source attribute).")}</li>
                        <li>{t_i18n("Copy the App Federation Metadata URL from section 3 and paste it as IdP Metadata URL above.")}</li>
                        <li>{t_i18n("Assign users or groups to the application.")}</li>
                      </ol>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
        {editing && <SettingsSaveBar dirty={dirty} saving={updateSSO.isPending} onSave={handleSave} onCancel={() => setEditing(false)} />}
      </CardContent>
    </Card>
  );
}
