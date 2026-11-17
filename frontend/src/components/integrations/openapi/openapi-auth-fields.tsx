import { Icon } from "@filigran/design-system";
/**
 * Choose how an OpenAPI integration authenticates, and enter that credential.
 *
 * Options come from the spec's security schemes (plus "no authentication" and
 * a manually entered bearer token). Unsupported schemes are listed below the
 * selector with their reason, so a spec whose only scheme is unsupported
 * still explains itself. A client certificate (mutual TLS) can be added to
 * any option; it is the credential of the mutual TLS option.
 */
import { useState, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SecretField } from "@/components/ui/secret-field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useFormatter } from "@/i18n";
import type { OpenAPIAuthOption } from "@/types";
import { authOptionLabel, type OpenAPICredentials } from "./openapi-auth";
import { ClientCertificateFields } from "./openapi-certificate-fields";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <Label className="block text-default-primary">{label}</Label>
      {children}
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded-sm bg-elevation-hover p-3">
      <Icon name="info" size={16} className="mt-px shrink-0 text-accent" />
      <p className="content-compact text-default-secondary">{children}</p>
    </div>
  );
}

export function OpenAPIAuthFields({
  options,
  value,
  onChange,
  credentials,
  onCredentialsChange,
  showSecrets,
  callbackUrl,
}: {
  options: OpenAPIAuthOption[];
  value: string;
  onChange: (optionId: string) => void;
  credentials: OpenAPICredentials;
  onCredentialsChange: (next: OpenAPICredentials) => void;
  showSecrets: boolean;
  /** Redirect URI to register for the redirect sign-in flows. */
  callbackUrl: string;
}) {
  const { t_i18n } = useFormatter();
  // Open from the start when a certificate is already filled in; the toggle is
  // the user's afterwards (collapsing keeps what was pasted).
  const [certificateOpen, setCertificateOpen] = useState(
    () => !!credentials.certificate || !!credentials.privateKey,
  );
  const supported = options.filter((o) => o.supported);
  const unsupported = options.filter((o) => !o.supported);
  const option = supported.find((o) => o.id === value);
  const kind = option?.kind;
  const set = (patch: Partial<OpenAPICredentials>) => onCredentialsChange({ ...credentials, ...patch });
  const secretType = showSecrets ? "text" : "password";
  const certificateShown = kind === "mutual_tls" || certificateOpen;
  const declaredScopes = Object.keys(option?.scopes ?? {});

  const clientFields = (secretLabel: string) => (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label={t_i18n("Client ID")}>
        <Input aria-label={t_i18n("Paste client ID...")} type={secretType} placeholder={t_i18n("Paste client ID...")} value={credentials.clientId} onChange={(e) => set({ clientId: e.target.value })} />
      </Field>
      <SecretField label={secretLabel} placeholder={t_i18n("Paste client secret...")} value={credentials.clientSecret} onChange={(clientSecret) => set({ clientSecret })} />
    </div>
  );
  const urlField = (label: string, key: "authorizeUrl" | "tokenUrl" | "deviceUrl") => (
    <Field label={label}>
      <Input aria-label={label} className="font-mono-plex content-compact" value={credentials[key]} onChange={(e) => set({ [key]: e.target.value })} />
    </Field>
  );
  const scopesField = (
    <Field label={t_i18n("Scopes (space-separated)")}>
      <Input aria-label={t_i18n("Scopes (space-separated)")} className="font-mono-plex content-compact" value={credentials.scopes} onChange={(e) => set({ scopes: e.target.value })} />
      {declaredScopes.length > 0 && (
        <p className="content-compact text-default-secondary">
          {t_i18n("Declared by the spec: {scopes}", { scopes: declaredScopes.join(", ") })}
        </p>
      )}
    </Field>
  );
  const loginFields = (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label={t_i18n("Username")}>
        <Input aria-label={t_i18n("Username")} value={credentials.username} onChange={(e) => set({ username: e.target.value })} autoComplete="off" />
      </Field>
      <Field label={t_i18n("Password")}>
        <Input aria-label={t_i18n("Password")} type={secretType} value={credentials.password} onChange={(e) => set({ password: e.target.value })} autoComplete="new-password" />
      </Field>
    </div>
  );
  const redirectNote = (
    <Note>
      {t_i18n("Register this redirect URI in the API's OAuth app, create the integration, then click Authorize:")}{" "}
      <code className="break-all text-accent">{callbackUrl}</code>
    </Note>
  );

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label className="block text-default-primary">{t_i18n("Authentication")}</Label>
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger aria-label={t_i18n("Authentication")}><SelectValue /></SelectTrigger>
          <SelectContent>
            {supported.map((o) => (
              <SelectItem key={o.id} value={o.id}>{authOptionLabel(o, t_i18n)}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {option?.description && <p className="content-compact text-default-secondary">{option.description}</p>}
      </div>

      {kind === "none" && (
        <div className="rounded-sm border border-alert-info bg-alert-info p-3">
          <p className="content-compact text-default-primary">{t_i18n("No credential is sent with the calls.")}</p>
        </div>
      )}

      {(kind === "api_key" || kind === "bearer") && (
        <div className="space-y-1">
          <SecretField
            label={kind === "api_key" ? t_i18n("API key") : t_i18n("Bearer token")}
            placeholder={kind === "api_key" ? t_i18n("Paste API key...") : t_i18n("Paste bearer token...")}
            value={credentials.apiKey}
            onChange={(apiKey) => set({ apiKey })}
          />
          {kind === "api_key" && option?.location && option.param_name && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Sent in the {location} as {name} on every call.", { location: option.location, name: option.param_name })}
            </p>
          )}
          {kind === "api_key" && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Optional: without a key, calls are sent without one. You can add it later from the Configuration tab.")}
            </p>
          )}
        </div>
      )}

      {kind === "basic" && loginFields}

      {(kind === "oauth2_authorization_code" || kind === "oauth2_client_credentials") && (
        <div className="space-y-3">
          {clientFields(t_i18n("Client Secret"))}
          {kind === "oauth2_authorization_code" && urlField(t_i18n("Authorization URL"), "authorizeUrl")}
          {urlField(t_i18n("Token URL"), "tokenUrl")}
          {scopesField}
          {kind === "oauth2_authorization_code" ? (
            redirectNote
          ) : (
            <p className="content-compact text-default-secondary">
              {t_i18n("Access tokens are requested from the token URL when tools run, and reused until they expire.")}
            </p>
          )}
        </div>
      )}

      {kind === "oauth2_implicit" && (
        <div className="space-y-3">
          <Field label={t_i18n("Client ID")}>
            <Input aria-label={t_i18n("Paste client ID...")} type={secretType} placeholder={t_i18n("Paste client ID...")} value={credentials.clientId} onChange={(e) => set({ clientId: e.target.value })} />
          </Field>
          {urlField(t_i18n("Authorization URL"), "authorizeUrl")}
          {scopesField}
          {redirectNote}
          <p className="content-compact text-default-secondary">
            {t_i18n("The implicit flow gives no refresh token: when the access token expires, click Authorize again.")}
          </p>
        </div>
      )}

      {kind === "oauth2_device" && (
        <div className="space-y-3">
          {clientFields(t_i18n("Client Secret (optional)"))}
          {urlField(t_i18n("Device authorization URL"), "deviceUrl")}
          {urlField(t_i18n("Token URL"), "tokenUrl")}
          {scopesField}
          <Note>
            {t_i18n("Create the integration, then click Authorize: a code appears, to enter on the API's sign-in page. No redirect URI is needed.")}
          </Note>
        </div>
      )}

      {kind === "oauth2_password" && (
        <div className="space-y-3">
          {loginFields}
          {clientFields(t_i18n("Client Secret (optional)"))}
          {urlField(t_i18n("Token URL"), "tokenUrl")}
          {scopesField}
          <p className="content-compact text-default-secondary">
            {t_i18n("The username and password are exchanged for access tokens at the token URL when tools run. The client ID and secret are only needed when the API requires them.")}
          </p>
        </div>
      )}

      {kind === "mutual_tls" && (
        <p className="content-compact text-default-secondary">
          {t_i18n("The API identifies the integration by its client certificate: paste it and its private key below.")}
        </p>
      )}

      <div className="rounded-sm border border-elevation-subtle-soft p-3">
        <button
          type="button"
          className="flex w-full items-center gap-1.5 text-left content-compact font-medium"
          onClick={() => setCertificateOpen(!certificateShown)}
          disabled={kind === "mutual_tls"}
        >
          {certificateShown ? <Icon name="chevron-down" size={16} /> : <Icon name="chevron-right" size={16} />}
          {kind === "mutual_tls" ? t_i18n("Client certificate") : t_i18n("Client certificate (optional, mutual TLS)")}
        </button>
        {certificateShown && (
          <div className="pt-3">
            <ClientCertificateFields
              value={{ certificate: credentials.certificate, privateKey: credentials.privateKey, passphrase: credentials.passphrase }}
              onChange={(next) => set(next)}
              showSecrets={showSecrets}
            />
          </div>
        )}
      </div>

      {unsupported.length > 0 && (
        <div className="space-y-1 rounded-sm bg-elevation-hover p-3">
          <p className="content-compact font-medium text-default-secondary">{t_i18n("Declared but not supported:")}</p>
          {unsupported.map((o) => (
            <p key={o.id} className="content-compact text-default-secondary">
              <span className="font-medium">{o.label}</span> — {o.unsupported_reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
