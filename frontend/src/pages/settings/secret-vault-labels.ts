// SPDX-License-Identifier: Apache-2.0

/** The copy of Settings > Access and security > Secret vaults, read through `t_i18n(TABLE[key])`. */

/** @i18n-keys */
export const VAULT_TYPE_LABELS = {
  hashicorp: "HashiCorp Vault",
  aws: "AWS Secrets Manager",
  gcp: "Google Cloud Secret Manager",
  azure: "Azure Key Vault",
  onepassword: "1Password",
} as const;

/** @i18n-keys */
export const VAULT_TYPE_DESCRIPTIONS = {
  hashicorp: "KV secrets engine, version 1 or 2, with an AppRole or a token. A Kubernetes login is declared in the environment.",
  aws: "Secrets read with the platform's AWS role, an assumed role or an access key.",
  gcp: "Secrets read with the workload's identity or a service account key.",
  azure: "Secrets read with a managed or workload identity, or an application's client secret.",
  onepassword: "Item fields read through a 1Password Connect server, one-time password seeds included.",
} as const;

/** @i18n-keys */
export const VAULT_METHOD_LABELS = {
  token: "Token",
  approle: "AppRole",
  default_chain: "The platform's AWS identity (IRSA, task role, instance profile)",
  assume_role: "Assume a role",
  access_key: "Access key",
  workload_identity: "Application Default Credentials / Workload Identity",
  service_account_key: "Service account key",
  managed_identity: "Managed or workload identity",
  client_secret: "Client secret",
  connect_token: "Connect server access token",
} as const;

/** @i18n-keys */
export const VAULT_METHOD_HELP = {
  token: "A Vault token with read (and list) on the paths users will pick. Prefer AppRole: a token is a long-lived credential.",
  approle: "The platform logs in with the role ID and secret ID and renews its token on its own.",
  default_chain: "Recommended on AWS: the role of the pod (IRSA, EKS Pod Identity), the task or the instance. Nothing to store.",
  assume_role: "The platform's identity assumes this role, renewed before it expires. The external ID is the one the role's trust policy requires.",
  access_key: "A long-lived key stored encrypted in the platform. Prefer a role where the platform runs on AWS.",
  workload_identity: "Recommended on Google Cloud: GKE Workload Identity or the attached service account. Nothing to store.",
  service_account_key: "A long-lived key stored encrypted in the platform. Prefer Workload Identity where the platform runs on Google Cloud.",
  managed_identity: "Recommended on Azure: AKS Workload Identity, an App Service or Container Apps identity, or a VM's managed identity. Nothing to store.",
  client_secret: "An application's client secret, stored encrypted in the platform. Prefer a managed identity where the platform runs on Azure.",
  connect_token: "An access token of your 1Password Connect server, granted read access to the vaults people will pick from. The 1Password integration may use the same server with a token of its own.",
} as const;

/** @i18n-keys */
export const VAULT_FIELD_LABELS = {
  address: "Address",
  namespace: "Namespace",
  token: "Token",
  role_id: "Role ID",
  secret_id: "Secret ID",
  auth_mount: "Auth mount",
  kv_version: "KV version",
  kv_mount: "KV mount",
  ca_cert: "CA certificate (PEM)",
  region: "Region",
  endpoint_url: "VPC endpoint URL",
  role_arn: "Role ARN",
  external_id: "External ID",
  access_key_id: "Access key ID",
  secret_access_key: "Secret access key",
  project: "Project",
  service_account_key: "Service account key (JSON)",
  vault_url: "Vault URL",
  tenant_id: "Tenant ID",
  client_id: "Client ID",
  client_secret: "Client secret",
} as const;

/** @i18n-keys */
const VAULT_FIELD_HELP = {
  address: "The https:// address of the Vault server. A plain http:// address is reported by the Security Audit.",
  namespace: "A Vault Enterprise namespace, if any.",
  role_id: "The role ID of the AppRole the platform signs in with.",
  auth_mount: "Where the auth method is mounted, when it is not the default.",
  kv_version: "The KV engine version at the mount.",
  kv_mount: "When set, picked paths are relative to this mount. Required with KV version 1.",
  ca_cert: "Paste the CA that signs the Vault certificate, for a private CA.",
  region: "Defaults to the region of the platform's environment.",
  endpoint_url: "Only when the platform reaches Secrets Manager through a VPC endpoint.",
  role_arn: "The ARN of the IAM role the platform assumes.",
  external_id: "Only when the role's trust policy requires one.",
  access_key_id: "The ID of the IAM access key, saved with its secret.",
  project: "The project of the secrets. Defaults to the project of the key or of the platform's identity.",
  vault_url: "The https:// URL of an Azure key vault: a host under vault.azure.net, or under a sovereign cloud's key vault domain.",
  tenant_id: "Defaults to the tenant of the workload identity.",
  client_id: "A user-assigned identity or the workload identity's application.",
} as const;

/** Field help that differs for one type, by `<type>.<field>`, read before `VAULT_FIELD_HELP`. @i18n-keys */
const VAULT_TYPE_FIELD_HELP = {
  "onepassword.address": "The https:// address of your 1Password Connect server (the one the 1Password integration uses works too).",
  "onepassword.ca_cert": "Paste the CA that signs the Connect server certificate, for a private CA.",
} as const;

/** The info bubble of each setting field: what it is, a real example, what empty does. @i18n-keys */
const VAULT_FIELD_INFO = {
  address: "The server the platform reads secrets from, e.g. https://vault.example.com:8200. Required: without it nothing is read.",
  namespace: "The Vault Enterprise namespace the secrets live in, e.g. admin/team. Empty: the root namespace.",
  role_id: "The role ID of the AppRole the platform signs in with, e.g. db02de05-fa39-4855-059b-67221c5c2f63. Required with AppRole.",
  auth_mount: "Where the AppRole auth method is mounted, e.g. approle-prod. Empty: approle.",
  kv_version: "The version of the KV secrets engine at the mount. It must match the engine, or reads fail. Empty: version 2.",
  kv_mount: "The mount of the KV engine, e.g. secret. Empty with version 2: each picked path starts with its mount. Required with version 1.",
  ca_cert: "The PEM of the CA that signs the server certificate, from -----BEGIN CERTIFICATE-----. Empty: the system's trusted CAs.",
  region: "The AWS region of the secrets, e.g. eu-west-1. Empty: the region of the platform's environment.",
  endpoint_url: "A Secrets Manager VPC endpoint, e.g. https://vpce-0a1b.secretsmanager.eu-west-1.vpce.amazonaws.com. Empty: the public regional endpoint.",
  role_arn: "The IAM role the platform assumes, e.g. arn:aws:iam::123456789012:role/xtm-one-secrets. Required with Assume a role.",
  external_id: "The external ID the role's trust policy requires, if any. Empty: none is sent.",
  access_key_id: "The ID of the IAM access key, e.g. AKIAIOSFODNN7EXAMPLE. Required with Access key, with its secret.",
  project: "The Google Cloud project of the secrets, e.g. my-project. Empty: the project of the key or of the platform's identity.",
  vault_url: "The URL of the Azure key vault, e.g. https://my-vault.vault.azure.net. Required: without it nothing is read.",
  tenant_id: "The Microsoft Entra tenant ID, e.g. 72f988bf-86f1-41af-91ab-2d7cd011db47. Empty with a managed identity: the identity's tenant. Required with Client secret.",
  client_id: "The application (client) ID, e.g. a user-assigned identity's. Empty with a managed identity: the system-assigned one. Required with Client secret.",
} as const;

/** The info bubble of a setting field. */
export function vaultFieldInfo(field: string): string | undefined {
  return VAULT_FIELD_INFO[field as keyof typeof VAULT_FIELD_INFO];
}

/** What each KV version means, shown under it in the picker. @i18n-keys */
export const VAULT_KV_VERSION_DESCRIPTIONS = {
  "2": "Versioned secrets, the default engine",
  "1": "Unversioned secrets, the latest value only",
} as const;

/** The help of a field of a type: its type-specific help, else its generic one. */
export function vaultFieldHelp(type: string, field: string): string | undefined {
  return (
    VAULT_TYPE_FIELD_HELP[`${type}.${field}` as keyof typeof VAULT_TYPE_FIELD_HELP] ??
    VAULT_FIELD_HELP[field as keyof typeof VAULT_FIELD_HELP]
  );
}

/** @i18n-keys */
export const VAULT_STATUS_LABELS = {
  connected: "Connected",
  failing: "Failing",
  disabled: "Disabled",
  not_checked: "Not tested yet",
  misconfigured: "Misconfigured",
  environment: "Declared in the environment",
  shadowed: "Hidden by the environment",
} as const;

/** @i18n-keys */
export const VAULT_STEP_LABELS = {
  configuration: "Settings",
  login: "Sign in",
  list: "List secrets",
  read: "Read a secret",
} as const;

/** @i18n-keys */
export const VAULT_USE_KINDS = {
  integration: "Integration",
  mcp_server: "MCP server",
  llm_provider: "LLM provider",
  variable: "Variable",
  channel_bot: "Channel bot",
  sso: "Single sign-on",
  langfuse: "Langfuse",
  browser_profile: "Browser profile",
} as const;

/** @i18n-keys */
export const VAULT_ACCESS_LABELS = {
  admins: "Administrators only",
  everyone: "Everyone",
  grants: "Specific groups and users",
} as const;

/** Where a secret field using a provider is opened, by the kind of entity holding it. */
export function vaultUseHref(kind: string, id: string): string | null {
  switch (kind) {
    case "integration":
      return `/integrations/${id}`;
    case "mcp_server":
      return `/mcp-servers/${id}`;
    case "variable":
      return `/connections/variables/${id}`;
    case "llm_provider":
      return "/settings/ai/models";
    case "channel_bot":
      return "/settings/integrations/channels";
    case "sso":
      return "/settings/security/authentication";
    case "langfuse":
      return "/settings/usage/observability";
    case "browser_profile":
      return "/connections";
    default:
      return null;
  }
}
