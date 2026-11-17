// SPDX-License-Identifier: Apache-2.0

import { AlertTriangle, ExternalLink, Radio, CheckCircle2, XCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { cn, lendsConnectedAccount } from "@/lib/utils";
import { INTEGRATION_STATUS, authorizedScopes, displayedIntegrationStatus, needsReauthorization, scopesNotGranted } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useIntegrationOpenAPISpec } from "@/hooks/api/useIntegrationApi";
import type { Integration, ProviderCatalogItem } from "@/types";

/** How each auth type is named, read by `t_i18n(AUTH_METHOD_LABELS[type])`. @i18n-keys */
const AUTH_METHOD_LABELS = {
  oauth: "OAuth 2.0",
  api_key: "API key",
  bot_token: "Bot token",
  platform_managed: "Managed by the platform",
  none: "None (unauthenticated)",
  client_credentials: "OAuth 2.0 client credentials",
  oauth_password: "OAuth 2.0 password grant",
} as const;
const AUTH_METHOD_LABEL_BY_TYPE: Readonly<Record<string, string>> = AUTH_METHOD_LABELS;

export function StatusCard({ integration, catalogEntry, onAuthorize }: { integration: Integration; catalogEntry?: ProviderCatalogItem; onAuthorize?: () => void }) {
  const { t_i18n } = useFormatter();
  const scopeDrift = needsReauthorization(integration);
  // Requested scopes the stored token was not granted (#9776): a declined consent box.
  const notGranted = scopesNotGranted(integration);
  // A declined scope is listed once, under "Requested scopes not granted", never as authorized.
  const shownScopes = authorizedScopes(integration);
  const effectiveStatus = displayedIntegrationStatus(integration);
  const awaitingReauthorization = effectiveStatus === "reauthorize";
  const statusCfg = INTEGRATION_STATUS[effectiveStatus] ?? INTEGRATION_STATUS.disconnected;
  const StatusIcon = statusCfg.icon;
  const needsFirstAuth = integration.auth_type === "oauth" && integration.status === "configured" && !integration.has_access_token;
  // An OpenAPI integration may present a client certificate (mutual TLS);
  // with no other credential, the certificate is how it authenticates.
  const hasClientCertificate = typeof integration.extra_config?.client_certificate === "string";
  // Its validity comes with the spec (shared with the API Spec tab's query).
  const certificate = useIntegrationOpenAPISpec(integration.id, hasClientCertificate && integration.provider === "openapi").data?.client_certificate;
  const authMethod = integration.auth_type === "none" && hasClientCertificate ? "Client certificate (mutual TLS)" : AUTH_METHOD_LABEL_BY_TYPE[integration.auth_type];
  // Spec-imported APIs often use public OAuth clients: a missing secret is not an error there.
  const missingSecretClass = catalogEntry?.spec_import ? "h-3 w-3 text-default-secondary" : "h-3 w-3 text-feedback-error-primary";

  // Compute scope differences for the warning
  const currentScopes = new Set((integration.scopes || "").split(/[\s,]+/).filter(Boolean));
  const requiredScopes = new Set((integration.required_scopes || "").split(/[\s,]+/).filter(Boolean));
  const addedScopes = [...requiredScopes].filter((s) => !currentScopes.has(s));
  const removedScopes = [...currentScopes].filter((s) => !requiredScopes.has(s));

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Radio className="h-4 w-4 text-accent" />
          {t_i18n("Connection status")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-3">
          <div className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-sm",
            effectiveStatus === "connected" ? "bg-feedback-success-primary/10" : effectiveStatus === "configured" || effectiveStatus === "reauthorize" ? "bg-feedback-alert-primary/10" : "bg-elevation-hover",
          )}>
            <StatusIcon className={cn("h-5 w-5", statusCfg.color)} />
          </div>
          <div>
            <p className="content-base font-medium">{t_i18n(statusCfg.label)}</p>
            <p className="content-compact text-default-secondary">
              {t_i18n("Authentication method:")} <span className="text-default-secondary">{authMethod ? t_i18n(authMethod) : integration.auth_type}</span>
            </p>
            {needsFirstAuth && (
              <p className="content-caption text-feedback-warning-primary mt-0.5">{t_i18n("OAuth credentials are set: authorize to complete the connection.")}</p>
            )}
            {scopeDrift && (
              <p className="content-caption text-feedback-warning-primary mt-0.5">{t_i18n("Integration permissions have changed: re-authorize to sync.")}</p>
            )}
            {!scopeDrift && notGranted.length > 0 && (
              <p className="content-caption text-feedback-warning-primary mt-0.5">{t_i18n("Some requested permissions were not granted: re-authorize and accept all of them.")}</p>
            )}
          </div>
        </div>

        {/* Credential badges */}
        <div className="rounded-sm bg-elevation-hover p-3">
          <div className="content-compact font-medium text-default-secondary mb-2">{t_i18n("Credentials")}</div>
          <div className="grid grid-cols-2 gap-2 content-compact">
            {integration.auth_type === "oauth" && (
              <>
                <div className="flex items-center gap-1.5">
                  {integration.has_client_id ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-feedback-error-primary" />}
                  {t_i18n("Client ID")}
                </div>
                <div className="flex items-center gap-1.5">
                  {integration.has_client_secret ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className={missingSecretClass} />}
                  {t_i18n("Client Secret")}
                </div>
                <div className="flex items-center gap-1.5">
                  {integration.has_access_token ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-default-secondary" />}
                  {t_i18n("Access Token (via OAuth)")}
                </div>
              </>
            )}
            {(integration.auth_type === "client_credentials" || integration.auth_type === "oauth_password") && (
              <>
                <div className="flex items-center gap-1.5">
                  {integration.has_client_id ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-default-secondary" />}
                  {t_i18n("Client ID")}
                </div>
                <div className="flex items-center gap-1.5">
                  {integration.has_client_secret ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-default-secondary" />}
                  {t_i18n("Client Secret")}
                </div>
                {integration.auth_type === "oauth_password" && (
                  <div className="flex items-center gap-1.5">
                    {integration.has_api_key ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-feedback-error-primary" />}
                    {t_i18n("Username and password")}
                  </div>
                )}
              </>
            )}
            {(integration.auth_type === "api_key" || integration.auth_type === "bot_token") && (
              <div className="flex items-center gap-1.5">
                {/* A spec-imported API's key is optional: without one, calls go out unauthenticated. */}
                {integration.has_api_key ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className={missingSecretClass} />}
                {integration.auth_type === "bot_token" ? t_i18n("Bot token") : t_i18n("API key")}
              </div>
            )}
            {hasClientCertificate && (
              <div className="flex items-center gap-1.5">
                {certificate?.expired ? (
                  <XCircle className="h-3 w-3 text-feedback-error-primary" />
                ) : certificate?.expires_soon ? (
                  <AlertTriangle className="h-3 w-3 text-feedback-warning-primary" />
                ) : (
                  <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />
                )}
                {t_i18n("Client certificate")}
                {certificate?.expired ? ` (${t_i18n("expired")})` : certificate?.expires_soon ? ` (${t_i18n("expires soon")})` : ""}
              </div>
            )}
            {integration.has_extra_config && (
              <div className="flex items-center gap-1.5">
                <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />
                {t_i18n("Extra config")}
              </div>
            )}
          </div>
          <VaultSecretsNotice
            status={integration.vault_secrets}
            fieldLabels={integration.auth_type === "bot_token" ? { api_key: t_i18n("Bot token") } : undefined}
            className="mt-3"
          />
        </div>

        {/* Scopes — highlight removed scopes with strikethrough when drift detected */}
        {shownScopes.length > 0 && (
          <div>
            <div className="content-compact font-medium text-default-secondary mb-2">{t_i18n("Authorized Scopes")}</div>
            <div className="flex flex-wrap gap-1.5" data-testid="authorized-scopes">
              {shownScopes.map((scope) => (
                <Badge
                  key={scope}
                  variant="outline"
                  className={cn(
                    "content-caption font-mono-plex",
                    scopeDrift && removedScopes.includes(scope) && "line-through opacity-50 border-feedback-error-primary/30 text-feedback-error-primary",
                  )}
                >
                  {scope}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {/* New scopes that need authorization */}
        {scopeDrift && addedScopes.length > 0 && (
          <div>
            <div className="content-compact font-medium text-feedback-warning-primary mb-2">{t_i18n("New Scopes (not yet authorized)")}</div>
            <div className="flex flex-wrap gap-1.5">
              {addedScopes.map((scope) => (
                <Badge key={scope} variant="outline" className="content-caption font-mono-plex border-feedback-alert-primary/30 text-feedback-warning-tertiary bg-feedback-alert-primary/5">
                  {scope}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {/* Requested scopes the stored token was not granted (#9776) */}
        {notGranted.length > 0 && (
          <div data-testid="scopes-not-granted">
            <div className="content-compact font-medium text-feedback-warning-primary mb-2">{t_i18n("Requested scopes not granted")}</div>
            <div className="flex flex-wrap gap-1.5">
              {notGranted.map((scope) => (
                <Badge key={scope} variant="outline" className="content-caption font-mono-plex border-feedback-alert-primary/30 text-feedback-warning-tertiary bg-feedback-alert-primary/5">
                  {scope}
                </Badge>
              ))}
            </div>
            <p className="content-caption text-default-secondary mt-2">
              {t_i18n("{count, plural, one {The account that signed in did not grant this scope: calls that need it fail until the integration is re-authorized with it accepted.} other {The account that signed in did not grant these # scopes: calls that need them fail until the integration is re-authorized with all of them accepted.}}", { count: notGranted.length })}
            </p>
          </div>
        )}

        {/* Authorize button — first auth, scope drift, a declined scope, or a grant the refresh marked dead */}
        {(needsFirstAuth || awaitingReauthorization) && onAuthorize && (
          <Button className="w-full gap-2" onClick={onAuthorize}>
            <ExternalLink className="h-4 w-4" />
            {t_i18n(awaitingReauthorization ? "Re-authorize with {provider}" : "Authorize with {provider}", { provider: catalogEntry?.name || integration.provider })}
          </Button>
        )}
        {(needsFirstAuth || awaitingReauthorization) && onAuthorize && lendsConnectedAccount(integration) && (
          <p className="text-[11px] text-default-secondary">{t_i18n("This integration is shared: authorizing it lets everyone it is shared with act using your credentials.")}</p>
        )}
        {/* Someone who connects their own account does so in "Your account". */}
        {(needsFirstAuth || awaitingReauthorization) && !onAuthorize && !integration.uses_own_credential && (
          <p className="text-[11px] text-default-secondary">
            {integration.is_company_managed
              ? t_i18n("Only an administrator can authorize this integration.")
              : t_i18n("Only the owner of this integration can authorize it: the token it stores is theirs.")}
          </p>
        )}

        {/* Scope drift warning with instructions */}
        {scopeDrift && (
          <div className="rounded-sm bg-feedback-alert-primary/5 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0" />
              <p className="content-compact font-medium text-feedback-warning-primary">{t_i18n("Permission changes detected")}</p>
            </div>
            <p className="content-caption text-feedback-warning-primary leading-relaxed">
              {t_i18n("The integration template has been updated with different permissions.")}
              {addedScopes.length > 0 && ` ${t_i18n("{count, plural, one {# scope added.} other {# scopes added.}}", { count: addedScopes.length })}`}
              {removedScopes.length > 0 && ` ${t_i18n("{count, plural, one {# scope removed.} other {# scopes removed.}}", { count: removedScopes.length })}`}
              {" "}{onAuthorize
                ? t_i18n("Re-authorize to sync your instance:")
                : integration.is_company_managed
                  ? t_i18n("An administrator has to re-authorize it to sync this instance.")
                  : t_i18n("Its owner has to re-authorize it to sync this instance.")}
            </p>
            {(addedScopes.length > 0 || onAuthorize) && (
              <ol className="content-caption text-feedback-warning-primary list-decimal list-inside space-y-1">
                {addedScopes.length > 0 && (
                  <li>{t_i18n("If using a custom OAuth app, update the API permissions in your {location} to include the new scopes", { location: integration.provider === "microsoft" ? t_i18n("Azure AD app registration") : t_i18n("app settings") })}</li>
                )}
                {onAuthorize && (
                  <li>{t_i18n("Click")} <strong>{t_i18n("Re-authorize")}</strong> {t_i18n("above to re-authenticate with the updated permissions")}</li>
                )}
              </ol>
            )}
          </div>
        )}

        {/* Timestamps */}
        <TimestampGrid createdAt={integration.created_at} updatedAt={integration.updated_at} />
      </CardContent>
    </Card>
  );
}
