/**
 * OpenAPI authentication options → the credential fields to ask for and the
 * integration payload they become. Kept out of the component file (Fast
 * Refresh: component modules export components only).
 *
 * The backend derives the integration's `auth_type` from the chosen option;
 * this module only decides which credential fields to send:
 *
 * - API key (optional) / bearer token → `api_key`;
 * - HTTP Basic → `api_key` = "username:password" (sent as `Authorization: Basic`);
 * - OAuth2 authorization code → client id / secret + authorize / token URLs + scopes;
 * - OAuth2 implicit → client id + authorize URL + scopes (the token comes back to the browser);
 * - OAuth2 device authorization → client id (secret optional) + device authorization
 *   URL (stored as the authorize URL) + token URL + scopes;
 * - OAuth2 client credentials → client id / secret + token URL + scopes;
 * - OAuth2 password → `api_key` = "username:password" + optional client id / secret
 *   + token URL + scopes;
 * - mutual TLS → nothing: the client certificate is the credential.
 *
 * A client certificate (PEM certificate, private key, optional passphrase)
 * can accompany any option; it goes into `extra_config`.
 */
import type { CreateIntegrationRequest, OpenAPIAuthOption } from "@/types";

export interface OpenAPICredentials {
  apiKey: string;
  username: string;
  password: string;
  clientId: string;
  clientSecret: string;
  authorizeUrl: string;
  tokenUrl: string;
  deviceUrl: string;
  scopes: string;
  certificate: string;
  privateKey: string;
  passphrase: string;
}

export function emptyCredentials(): OpenAPICredentials {
  return {
    apiKey: "",
    username: "",
    password: "",
    clientId: "",
    clientSecret: "",
    authorizeUrl: "",
    tokenUrl: "",
    deviceUrl: "",
    scopes: "",
    certificate: "",
    privateKey: "",
    passphrase: "",
  };
}

/** Pre-fill what the spec declares (OAuth URLs and the scopes to request). */
export function prefillCredentials(option: OpenAPIAuthOption | undefined, current: OpenAPICredentials): OpenAPICredentials {
  if (!option) return current;
  return {
    ...current,
    authorizeUrl: option.authorize_url ?? "",
    tokenUrl: option.token_url ?? "",
    deviceUrl: option.device_authorization_url ?? "",
    scopes: (option.default_scopes ?? Object.keys(option.scopes ?? {})).join(" "),
  };
}

function login(creds: OpenAPICredentials): string | undefined {
  return creds.username.trim() || creds.password ? `${creds.username.trim()}:${creds.password}` : undefined;
}

/** The credential part of the create payload for *option* (the certificate aside). */
export function credentialPayload(
  option: OpenAPIAuthOption | undefined,
  creds: OpenAPICredentials,
): Partial<CreateIntegrationRequest> {
  const client = {
    client_id: creds.clientId.trim() || undefined,
    client_secret: creds.clientSecret.trim() || undefined,
  };
  switch (option?.kind) {
    case "api_key":
    case "bearer":
      return { api_key: creds.apiKey.trim() || undefined };
    case "basic": {
      const value = login(creds);
      return value ? { api_key: value } : {};
    }
    case "oauth2_authorization_code":
      return {
        ...client,
        authorize_url: creds.authorizeUrl.trim() || undefined,
        token_url: creds.tokenUrl.trim() || undefined,
        scopes: creds.scopes.trim(),
      };
    case "oauth2_implicit":
      return {
        client_id: client.client_id,
        authorize_url: creds.authorizeUrl.trim() || undefined,
        scopes: creds.scopes.trim(),
      };
    case "oauth2_device":
      return {
        ...client,
        // The device authorization endpoint is where this sign-in starts.
        authorize_url: creds.deviceUrl.trim() || undefined,
        token_url: creds.tokenUrl.trim() || undefined,
        scopes: creds.scopes.trim(),
      };
    case "oauth2_client_credentials":
      return {
        ...client,
        token_url: creds.tokenUrl.trim() || undefined,
        scopes: creds.scopes.trim(),
      };
    case "oauth2_password":
      return {
        ...client,
        api_key: login(creds),
        token_url: creds.tokenUrl.trim() || undefined,
        scopes: creds.scopes.trim(),
      };
    default:
      return {};
  }
}

/** The client certificate as `extra_config` entries (empty when none is entered). */
export function certificateConfig(creds: OpenAPICredentials): Record<string, string> {
  if (!creds.certificate.trim() && !creds.privateKey.trim()) return {};
  return {
    client_certificate: creds.certificate.trim(),
    client_private_key: creds.privateKey.trim(),
    ...(creds.passphrase ? { client_private_key_password: creds.passphrase } : {}),
  };
}

/** What `credentialProblem` answers, each a catalog key. @i18n-keys */
const CREDENTIAL_PROBLEMS = {
  certificatePair: "Paste both the client certificate and its private key.",
  bearer: "Enter the bearer token.",
  username: "Enter the username.",
  clientId: "Enter the OAuth client ID.",
  codeUrls: "Enter the authorization and token URLs.",
  authorizeUrl: "Enter the authorization URL.",
  deviceUrls: "Enter the device authorization and token URLs.",
  clientPair: "Enter the OAuth client ID and secret.",
  tokenUrl: "Enter the token URL.",
  certificate: "Paste the client certificate and its private key.",
} as const;

/** Why the credentials cannot be saved yet (an i18n key), or null. */
export function credentialProblem(option: OpenAPIAuthOption | undefined, creds: OpenAPICredentials): string | null {
  const hasCertificate = !!creds.certificate.trim();
  const hasKey = !!creds.privateKey.trim();
  if (hasCertificate !== hasKey) return CREDENTIAL_PROBLEMS.certificatePair;
  switch (option?.kind) {
    case "api_key":
      return null;
    case "bearer":
      return creds.apiKey.trim() ? null : CREDENTIAL_PROBLEMS.bearer;
    case "basic":
      return creds.username.trim() ? null : CREDENTIAL_PROBLEMS.username;
    case "oauth2_authorization_code":
      if (!creds.clientId.trim()) return CREDENTIAL_PROBLEMS.clientId;
      if (!creds.authorizeUrl.trim() || !creds.tokenUrl.trim()) return CREDENTIAL_PROBLEMS.codeUrls;
      return null;
    case "oauth2_implicit":
      if (!creds.clientId.trim()) return CREDENTIAL_PROBLEMS.clientId;
      return creds.authorizeUrl.trim() ? null : CREDENTIAL_PROBLEMS.authorizeUrl;
    case "oauth2_device":
      if (!creds.clientId.trim()) return CREDENTIAL_PROBLEMS.clientId;
      if (!creds.deviceUrl.trim() || !creds.tokenUrl.trim()) return CREDENTIAL_PROBLEMS.deviceUrls;
      return null;
    case "oauth2_client_credentials":
      if (!creds.clientId.trim() || !creds.clientSecret.trim()) return CREDENTIAL_PROBLEMS.clientPair;
      if (!creds.tokenUrl.trim()) return CREDENTIAL_PROBLEMS.tokenUrl;
      return null;
    case "oauth2_password":
      if (!creds.username.trim()) return CREDENTIAL_PROBLEMS.username;
      if (creds.clientSecret.trim() && !creds.clientId.trim()) return CREDENTIAL_PROBLEMS.clientId;
      return creds.tokenUrl.trim() ? null : CREDENTIAL_PROBLEMS.tokenUrl;
    case "mutual_tls":
      return hasCertificate ? null : CREDENTIAL_PROBLEMS.certificate;
    default:
      return null;
  }
}

/** Options the platform adds to every spec; their labels are i18n keys. */
const SYNTHETIC_OPTION_IDS = new Set(["none", "manual_bearer"]);

/**
 * An option's display label. Spec-declared labels are generated from the
 * document (they name its scheme), so they are shown as they are; only the
 * platform's own options are translated.
 */
export function authOptionLabel(option: OpenAPIAuthOption, t: (id: string) => string): string {
  if (SYNTHETIC_OPTION_IDS.has(option.id)) return t(option.label);
  // Name the scheme only when the label does not already say it (an API
  // key's label quotes its parameter, which is often the scheme's name too).
  return option.scheme_name && !option.label.includes(option.scheme_name)
    ? `${option.label} — ${option.scheme_name}`
    : option.label;
}

/** Display a tool name under an edited prefix (the backend recomputes the real one). */
export function renamePrefix(toolName: string | null | undefined, from: string, to: string): string | null | undefined {
  if (!toolName || from === to) return toolName;
  const rest = from && toolName.startsWith(`${from}_`) ? toolName.slice(from.length + 1) : toolName;
  const normalized = to.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 16);
  return normalized ? `${normalized}_${rest}` : rest;
}
