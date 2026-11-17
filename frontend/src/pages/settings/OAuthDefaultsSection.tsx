// SPDX-License-Identifier: Apache-2.0

import { platformOrigin } from "@/lib/base-path";
import { useState } from "react";
import { IconButton, Spinner } from "@filigran/design-system";
import {
  Check,
  CheckCircle2,
  Plus,
  Pencil,
  Trash2,
  Plug2,
  Eye,
  EyeOff,
  Webhook,
  Copy,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, focusFirstInvalidField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useOAuthDefaults, useUpsertOAuthDefault, useDeleteOAuthDefault } from "@/hooks/api/useSettingsApi";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { EXTRA_CONFIG_MASK, maskedStoredSecrets } from "@/lib/extra-fields";
import { useFormatter } from "@/i18n";
import { oauthDefaultWebhookPath } from "@/lib/slack-events";

// ---------------------------------------------------------------------------
// OAuth Defaults Management (admin section)
// ---------------------------------------------------------------------------

// Each provider tile is a framed card (the Catalog tile recipe); the blocks
// nested inside it (shared webhook URL, polling note, inline form) are inset
// panels with their own boundary, so they never read as a flat tint. The
// status and actions row sits right under the fixed-height header, so it lines
// up across a grid row whatever the tile holds below it.
const INSET_PANEL_CLASS = "mt-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3";

// Example values shown in the empty extra fields (formats, not words).
const TENANT_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000";
const COMPOSE_STYLE_PLACEHOLDER = "font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt";

export function OAuthDefaultsSection() {
  const { t_i18n } = useFormatter();
  const { data: oauthDefaults } = useOAuthDefaults();
  const { data: catalog } = useProviderCatalog("integration");
  const { data: authConfig } = useAuthConfig();
  const upsert = useUpsertOAuthDefault();
  const remove = useDeleteOAuthDefault();

  const [configuring, setConfiguring] = useState<string | null>(null);
  const [showSecrets, setShowSecrets] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [extraFields, setExtraFields] = useState<Record<string, string>>({});
  const [copiedWebhookUrl, setCopiedWebhookUrl] = useState<string | null>(null);
  // Set by a refused Save: the empty required fields then show their inline
  // error, and the error clears as soon as the field is filled.
  const [saveAttempted, setSaveAttempted] = useState(false);

  const baseUrl = authConfig?.base_url || platformOrigin();

  // Only show OAuth-capable providers
  const oauthProviders = catalog?.filter((c) => c.auth_type === "oauth") ?? [];

  const clientIdError = saveAttempted && !clientId.trim() ? t_i18n("Client ID is required") : undefined;
  const clientSecretError = saveAttempted && !clientSecret.trim() ? t_i18n("Client Secret is required") : undefined;

  const handleSave = () => {
    if (!configuring) return;
    if (!clientId.trim() || !clientSecret.trim()) {
      setSaveAttempted(true);
      focusFirstInvalidField([
        !clientId.trim() && `oauth-default-${configuring}-client-id`,
        !clientSecret.trim() && `oauth-default-${configuring}-client-secret`,
      ]);
      return;
    }
    upsert.mutate(
      {
        provider: configuring,
        client_id: clientId,
        client_secret: clientSecret,
        extra_config: Object.keys(extraFields).length > 0 ? extraFields : undefined,
      },
      {
        onSuccess: () => {
          setConfiguring(null);
          setClientId("");
          setClientSecret("");
          setExtraFields({});
          setShowSecrets(false);
          setSaveAttempted(false);
        },
      },
    );
  };

  // A short translated label, a helper line and an info bubble for every extra
  // field the catalog declares on an OAuth provider. A key added to the catalog
  // later keeps the catalog's own description as its label until it is listed.
  // `secret` marks the values typed masked behind the Show toggle; every other
  // listed key is plain text. A key not listed stays masked: it may be a secret.
  const knownExtraField = (key: string, providerName: string): { label: string; helper: string; info: string; placeholder?: string; secret?: boolean } | undefined => {
    switch (key) {
      case "tenant_id":
        return {
          label: t_i18n("Tenant ID"),
          helper: t_i18n("The Directory (tenant) ID of the app registration in Microsoft Entra."),
          info: t_i18n("A GUID, for example 3f2b8c1e-0000-4000-8000-1a2b3c4d5e6f, in Microsoft Entra admin center > App registrations > Overview. Sign-in uses this tenant instead of the common endpoint. Leave it empty for a multi-tenant app, unless this provider requires a tenant."),
          placeholder: TENANT_ID_PLACEHOLDER,
        };
      case "compose_style":
        return {
          label: t_i18n("Email compose style"),
          helper: t_i18n("Optional CSS for the emails the agents write. Empty keeps Outlook's own style, none sends no style."),
          info: t_i18n("Inline CSS applied to the body of the emails the agents write through Outlook, for example font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt;color:rgb(0,0,0). Empty keeps Outlook's own style. Type none to send no style."),
          placeholder: COMPOSE_STYLE_PLACEHOLDER,
        };
      case "webhook_secret":
        return {
          secret: true,
          label: t_i18n("Webhook secret"),
          helper: t_i18n("The secret {provider} signs its events with, so forged events are refused.", { provider: providerName }),
          info: t_i18n("Copied from the app settings at {provider}: the Signing Secret under Basic Information for Slack, the webhook secret set on the app for GitHub. Every inbound event is checked against it. Empty: events are accepted unverified, or polled instead where verification is required.", { provider: providerName }),
        };
      case "subscription_id":
        return {
          label: t_i18n("Subscription ID"),
          helper: t_i18n("The Azure subscription that holds the Sentinel workspace."),
          info: t_i18n("A GUID, in Azure portal > Subscriptions. With the resource group and the workspace name, it locates the Log Analytics workspace the agents query."),
          placeholder: TENANT_ID_PLACEHOLDER,
        };
      case "resource_group":
        return {
          label: t_i18n("Resource group"),
          helper: t_i18n("The Azure resource group that holds the Sentinel workspace."),
          info: t_i18n("The name shown in Azure portal > Resource groups, for example rg-security. Needed with the subscription ID and the workspace name."),
        };
      case "workspace_name":
        return {
          label: t_i18n("Workspace name"),
          helper: t_i18n("The Log Analytics workspace behind Microsoft Sentinel."),
          info: t_i18n("The name shown in Azure portal > Log Analytics workspaces, for example law-sentinel-prod. Incidents and queries are read from this workspace."),
        };
      case "environment":
        return {
          label: t_i18n("Environment"),
          helper: t_i18n("The {provider} platform the account lives on.", { provider: providerName }),
          info: t_i18n("Production sends real envelopes from account.docusign.com. Developer (demo) uses the free test platform account-d.docusign.com, for trials only."),
        };
      case "account_id":
        return {
          label: t_i18n("Account ID"),
          helper: t_i18n("The {provider} API account ID. Empty uses each user's default account.", { provider: providerName }),
          info: t_i18n("A GUID, in {provider} Admin > Apps and Keys > API Account ID. Set it when users belong to several accounts.", { provider: providerName }),
          placeholder: TENANT_ID_PLACEHOLDER,
        };
      case "integration_key":
        return {
          secret: true,
          label: t_i18n("Integration key"),
          helper: t_i18n("For JWT Grant mode only, with the user ID below."),
          info: t_i18n("The Integration Key of the app, a GUID, in {provider} Admin > Apps and Keys. With a user ID, agents act without an interactive sign-in. Empty: users sign in through OAuth.", { provider: providerName }),
          placeholder: TENANT_ID_PLACEHOLDER,
        };
      case "user_id":
        return {
          label: t_i18n("User ID"),
          helper: t_i18n("The user the agents act as in JWT Grant mode."),
          info: t_i18n("The API Username of that user, a GUID, in {provider} Admin > Apps and Keys. Used with the integration key. Empty: users sign in through OAuth.", { provider: providerName }),
          placeholder: TENANT_ID_PLACEHOLDER,
        };
      case "connect_hmac_key":
        return {
          secret: true,
          label: t_i18n("Connect HMAC key"),
          helper: t_i18n("Verifies the real-time envelope events {provider} Connect sends.", { provider: providerName }),
          info: t_i18n("In {provider} Admin > Connect > HMAC keys. While rotating, list several keys separated by commas. Empty: Connect events cannot be verified.", { provider: providerName }),
        };
      case "account_name":
        return {
          label: t_i18n("Account name"),
          helper: t_i18n("The Microsoft Purview account, needed only for the Data Map and data catalog tools."),
          info: t_i18n("The first part of the Purview address, for example mycompany for mycompany.purview.azure.com. Empty: the Data Map and data catalog tools are not available."),
        };
      case "account_type":
        return {
          label: t_i18n("Account type"),
          helper: t_i18n("Whether the agents post as a person or as a page."),
          info: t_i18n("Personal account posts as the user who connects {provider}. Page posts as a page or organisation that user manages.", { provider: providerName }),
        };
      default:
        return undefined;
    }
  };

  // The translated label and one-line meaning of the choices the catalog
  // declares on an extra field; a choice not listed keeps the catalog's label.
  const knownExtraOption = (key: string, value: string): { label: string; description?: string } | undefined => {
    switch (`${key}:${value}`) {
      case "environment:production":
        return { label: t_i18n("Production"), description: t_i18n("Real envelopes, account.docusign.com") };
      case "environment:demo":
        return { label: t_i18n("Developer (demo)"), description: t_i18n("Test envelopes, account-d.docusign.com") };
      case "account_type:personal":
        return { label: t_i18n("Personal account") };
      case "account_type:page":
        return { label: t_i18n("Page") };
      default:
        return undefined;
    }
  };

  const startEditing = (provider: string) => {
    setConfiguring(provider);
    setSaveAttempted(false);
    setClientId("");
    setClientSecret("");
    // A stored secret left empty is saved as the mask, which keeps it.
    setExtraFields(maskedStoredSecrets(oauthDefaults?.find((d) => d.provider === provider)?.stored_secret_keys));
    setShowSecrets(false);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Plug2 className="h-4 w-4 text-accent" />
          {t_i18n("OAuth Defaults")}
        </CardTitle>
        <CardDescription>
          {t_i18n("Pre-configure OAuth credentials (Client ID & Secret) for integration providers. Users will be able to connect integrations with a single click instead of registering their own OAuth app.")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 items-stretch gap-3">
          {oauthProviders.map((provider) => {
            const existing = oauthDefaults?.find((d) => d.provider === provider.provider);
            const isEditing = configuring === provider.provider;

            return (
              <div
                key={provider.provider}
                data-testid={`oauth-default-card-${provider.provider}`}
                // The tile being configured spans the whole grid row, so its
                // form has the width of the card and Cancel and Save stay on
                // one row in every language.
                className={`flex h-full flex-col rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-4 transition-colors duration-200 hover:border-accent/30${isEditing ? " sm:col-span-2 lg:col-span-3" : ""}`}
              >
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
                    {provider.logo ? (
                      <img src={provider.logo} alt={provider.name} className="h-5 w-5" />
                    ) : (
                      <Plug2 className="h-4 w-4 text-default-secondary" />
                    )}
                  </div>
                  <div className="min-w-0">
                    <p className="content-base font-medium truncate">{provider.name}</p>
                    <p className="content-caption text-default-secondary line-clamp-1">{provider.description}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2 mt-3 pt-3 border-t border-elevation-subtle-soft">
                  {existing ? (
                    <>
                      <Badge variant="success" className="content-caption">
                        <CheckCircle2 className="h-3 w-3 mr-1" />
                        {t_i18n("Configured")}
                      </Badge>
                      <div className="ml-auto flex items-center gap-1">
                        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Edit OAuth defaults")} icon={<Pencil className="h-3 w-3" />} className="h-7 w-7" onClick={() => startEditing(provider.provider)} />
                        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Remove OAuth defaults")} icon={<Trash2 className="h-3 w-3" />} className="h-7 w-7 text-default-secondary hover:text-feedback-error-primary" onClick={() => remove.mutate(provider.provider)} />
                      </div>
                    </>
                  ) : (
                    <Button
                      variant="secondary"
                      size="sm"
                      className="gap-1.5 content-compact w-full"
                      onClick={() => startEditing(provider.provider)}
                    >
                      <Plus className="h-3 w-3" />
                      {t_i18n("Configure")}
                    </Button>
                  )}
                </div>

                {/* Shared webhook URL – only for providers that use a single app-level webhook (e.g. GitHub, Slack) */}
                {existing && provider.needs_shared_webhook && (() => {
                  const webhookPath = oauthDefaultWebhookPath(provider);
                  if (!webhookPath) {
                    return (
                      <div className={`${INSET_PANEL_CLASS} space-y-2`}>
                        <div className="flex items-center gap-2">
                          <Webhook className="h-3 w-3 text-accent" />
                          <span className="content-compact font-medium">{t_i18n("{provider} Events", { provider: provider.name })}</span>
                        </div>
                        <p className="content-caption text-default-secondary leading-relaxed">
                          {t_i18n("New Slack messages are picked up by polling every 5 minutes.")}
                        </p>
                      </div>
                    );
                  }
                  const providerWebhookUrl = `${baseUrl}${webhookPath}`;
                  const isSlack = provider.provider === "slack";
                  const isCopied = copiedWebhookUrl === provider.provider;
                  return (
                    <div className={`${INSET_PANEL_CLASS} space-y-2`}>
                      <div className="flex items-center gap-2">
                        <Webhook className="h-3 w-3 text-accent" />
                        <span className="content-compact font-medium">{t_i18n("Shared Webhook URL")}</span>
                      </div>
                      <p className="content-caption text-default-secondary leading-relaxed">
                        {isSlack
                          ? t_i18n("Set this URL as the Request URL in the Event Subscriptions of this Slack app. Store the app's Signing Secret in the Signing secret field of this default first: Slack verifies the URL with a signed request, and unsigned events are refused.")
                          : t_i18n("Configure this URL once in {provider}. All users' agents will receive events automatically.", { provider: provider.name })}
                      </p>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default px-2 py-1 content-caption text-accent font-mono-plex break-all select-all">
                          {providerWebhookUrl}
                        </code>
                        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Copy webhook URL")} icon={isCopied ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />} className="h-6 w-6 shrink-0" onClick={() => { navigator.clipboard.writeText(providerWebhookUrl); setCopiedWebhookUrl(provider.provider); setTimeout(() => setCopiedWebhookUrl(null), COPY_FEEDBACK_DELAY); }} />
                      </div>
                    </div>
                  );
                })()}

                {/* Inline config form */}
                {isEditing && (
                  <div className={`${INSET_PANEL_CLASS} space-y-3`} data-testid={`oauth-default-form-${provider.provider}`}>
                    <div className="flex items-center justify-between">
                      <h4 className="content-compact font-medium">
                        {existing ? t_i18n("Update") : t_i18n("Configure")}
                      </h4>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-2 content-caption"
                        onClick={() => setShowSecrets(!showSecrets)}
                      >
                        {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
                        {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
                      </Button>
                    </div>

                    <div className="space-y-3">
                      <SettingsField
                        id={`oauth-default-${provider.provider}-client-id`}
                        label={t_i18n("Client ID")}
                        helper={t_i18n("The client ID of the OAuth app registered at {provider}.", { provider: provider.name })}
                        info={t_i18n("Found in the app registration at {provider}, often named Client ID, Application ID or App ID. Every user who connects {provider} signs in through this app. When no default is saved, each user registers their own OAuth app.", { provider: provider.name })}
                        error={clientIdError}
                        required
                      >
                        <Input
                          id={`oauth-default-${provider.provider}-client-id`}
                          aria-label={t_i18n("Client ID")}
                          aria-required="true"
                          aria-describedby={settingsFieldHintId(`oauth-default-${provider.provider}-client-id`)}
                          type="text"
                          placeholder={t_i18n("Paste client ID...")}
                          value={clientId}
                          onChange={(e) => setClientId(e.target.value)}
                        />
                      </SettingsField>
                      <SettingsField
                        id={`oauth-default-${provider.provider}-client-secret`}
                        label={t_i18n("Client Secret")}
                        helper={t_i18n("The secret of the same OAuth app. Users connecting {provider} never see it.", { provider: provider.name })}
                        info={t_i18n("Generated with the app registration at {provider}, shown once by most providers. Saved encrypted and sent only to {provider} when a user signs in. When no default is saved, each user registers their own OAuth app.", { provider: provider.name })}
                        error={clientSecretError}
                        required
                      >
                        <Input
                          id={`oauth-default-${provider.provider}-client-secret`}
                          aria-label={t_i18n("Client Secret")}
                          aria-required="true"
                          aria-describedby={settingsFieldHintId(`oauth-default-${provider.provider}-client-secret`)}
                          type={showSecrets ? "text" : "password"}
                          placeholder={t_i18n("Paste client secret...")}
                          value={clientSecret}
                          onChange={(e) => setClientSecret(e.target.value)}
                        />
                      </SettingsField>
                    </div>

                    {/* Extra fields (e.g. tenant_id for Microsoft) */}
                    {provider.extra_fields && Object.entries(provider.extra_fields).map(([key, desc]) => {
                      const options = provider.extra_field_options?.[key]?.map((option) => {
                        const knownOption = knownExtraOption(key, option.value);
                        return knownOption ? { ...option, ...knownOption } : option;
                      });
                      const known = knownExtraField(key, provider.name);
                      const fieldId = `oauth-default-${provider.provider}-${key}`;
                      return (
                        <SettingsField key={key} id={fieldId} label={known?.label ?? desc} helper={known?.helper} info={known?.info}>
                          {options ? (
                            <SearchableSelect
                              aria-labelledby={settingsFieldLabelId(fieldId)}
                              searchable="auto"
                              sort={false}
                              options={options}
                              value={extraFields[key] || options[0]?.value || ""}
                              onValueChange={(v) => setExtraFields({ ...extraFields, [key]: v })}
                            />
                          ) : (
                            <Input
                              id={fieldId}
                              aria-label={known?.label ?? desc}
                              aria-describedby={known ? settingsFieldHintId(fieldId) : undefined}
                              type={showSecrets || (known && !known.secret) ? "text" : "password"}
                              placeholder={extraFields[key] === EXTRA_CONFIG_MASK ? t_i18n("Stored: type to replace") : known?.placeholder}
                              value={extraFields[key] === EXTRA_CONFIG_MASK ? "" : extraFields[key] || ""}
                              onChange={(e) => setExtraFields({ ...extraFields, [key]: e.target.value })}
                            />
                          )}
                        </SettingsField>
                      );
                    })}

                    <div className="flex flex-wrap justify-end gap-2">
                      <Button variant="secondary" onClick={() => { setConfiguring(null); setSaveAttempted(false); }} disabled={upsert.isPending}>
                        {t_i18n("Cancel")}
                      </Button>
                      <Button
                        onClick={handleSave}
                        disabled={upsert.isPending}
                        aria-busy={upsert.isPending || undefined}
                      >
                        {upsert.isPending ? (
                          <><Spinner size="sm" tone="inherit" />{t_i18n("Saving...")}</>
                        ) : (
                          t_i18n("Save")
                        )}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
