# Secret vaults

XTM One can read secrets from your enterprise vault instead of storing them. You connect a vault once, grant it to the people who may use it, and they then pick a secret from it in any secret field: the field stores a **reference** to the secret, and the platform reads the secret from the vault each time a feature uses it. Rotating the secret in the vault is enough: nothing changes in XTM One and no restart is needed.

Five vaults are supported: HashiCorp Vault, AWS Secrets Manager, Google Cloud Secret Manager, Azure Key Vault and 1Password (through a 1Password Connect server). The feature is opt-in: until a vault is connected, a secret field only takes a value.

## Connect a vault

Go to **Settings > Access and security > Secret vaults** and click **Connect a vault**. Administrators only.

1. Pick the type: HashiCorp Vault, AWS Secrets Manager, Google Cloud Secret Manager, Azure Key Vault or 1Password.
2. Give it a **display name** (what people see in the picker) and a **reference name**: lowercase letters, digits and `_`, starting with a letter, 32 at most. References name the vault by it (`vault://<reference name>/...`). It cannot change once a secret field uses the vault, and it cannot be the name of a vault [declared in the environment](#advanced-declare-a-vault-in-the-environment).
3. Choose the **sign-in method** and fill in its settings (below). The form recommends the workload identity of the platform where the type has one: nothing is stored then.
4. Click **Test connection**. The test reports each step with its reason when it fails, never a value: the settings are usable, the platform signs in, it can list secrets, it can read one (the first secret the listing finds, read and dropped). For HashiCorp Vault it also lists the KV mounts the token sees, to fill in the mount.
5. Set **who may use it** ([Grant a vault](#grant-a-vault)), then click **Connect**. The vault is tested again once saved, and its status shows on the list.

What opens the vault (a token, an AppRole secret ID, an access key, a service account key, a client secret) is stored encrypted, like every other credential of the platform. It is write-only: the form says it is **Set**, with **Replace** and **Clear**, and no API ever returns it. It is never logged or traced.

The list shows every vault with its type, its status (**Connected**, **Failing** with the reason, **Not tested yet**, **Disabled**, **Misconfigured**, or **Declared in the environment**), when it was last tested, and how many secret fields read from it (a count up to 10 seconds old: finding the fields decrypts every stored secret, so the list reuses a recent count). Click that count to see which integrations, MCP servers, variables, LLM providers, channel bots, browser profiles and settings use it. From the list you can **Test** a vault, **Edit** it, switch it off and on, or **Delete** it. A vault a secret field reads from cannot be deleted: the platform lists the fields to change first. A disabled vault refuses new references, and every read of it fails until it is enabled again. Disabling is also the way to stop references at once: removing a grant does not revoke the references already stored ([Grant a vault](#grant-a-vault)).

Every connection, change, enable, disable, test and deletion is recorded in the audit log.

### HashiCorp Vault

| Setting | Description |
|---|---|
| Address | **Required.** The `https://` address of the Vault server, for example `https://vault.example.com:8200`. |
| Namespace | A Vault Enterprise namespace. |
| Sign-in method | **AppRole** (role ID and secret ID; the platform renews its token on its own), or **Token** (a long-lived token: prefer AppRole). |
| Role ID, Secret ID, Auth mount | With AppRole: the role ID and the secret ID (required), and where the method is mounted (`approle` by default). |
| KV version | `2` (default) or `1`. |
| KV mount | When set, picked paths are relative to this mount. **Required** with KV version 1. Otherwise the first segment of a path is the mount, as with `vault kv get`. |
| CA certificate (PEM) | Paste the CA that signs the Vault certificate, for a private CA. |

The platform only reads secrets from a KV engine: a path whose first segment is one of Vault's own mounts (`sys`, `auth`, `identity`, `cubbyhole`) is refused. The token needs `read` (and `list`, for browsing) on the paths users will pick.

To sign in with the platform's **Kubernetes service account**, [declare the vault in the environment](#advanced-declare-a-vault-in-the-environment) (`AUTH_METHOD=kubernetes`). That login sends the pod's service account token to the vault address, so only the operator who deploys the platform chooses that address, never a session in the UI.

### AWS Secrets Manager

| Setting | Description |
|---|---|
| Region | The region of the secrets. Empty: the region of the platform's environment. |
| VPC endpoint URL | Only when the platform reaches Secrets Manager through a VPC endpoint. |
| Sign-in method | **The platform's AWS identity** (recommended: an IRSA or EKS Pod Identity role, an ECS task role, an instance profile; nothing stored), **Assume a role** (the role ARN and an optional external ID; the platform's identity assumes it and renews it before it expires), or **Access key** (an access key ID and secret access key, stored encrypted: prefer a role). |

The identity needs `secretsmanager:GetSecretValue` on the secrets users pick, `secretsmanager:ListSecrets` for browsing, and `kms:Decrypt` when they are encrypted with a customer-managed key. A path is the secret's name or ARN; names holding `/` are browsed as folders.

### Google Cloud Secret Manager

| Setting | Description |
|---|---|
| Project | The project of the secrets. Empty: the project of the service account key, or of the platform's identity. |
| Sign-in method | **Application Default Credentials / Workload Identity** (recommended: GKE Workload Identity or the attached service account; nothing stored), or **Service account key** (paste or upload the JSON key, stored encrypted; it is exchanged at Google's token endpoint, whatever its `token_uri` says). |

The service account needs `roles/secretmanager.secretAccessor` on the secrets users pick, and `roles/secretmanager.viewer` on the project for browsing. A path is `<secret>`, `<secret>/<version>` or `projects/<project>/secrets/<secret>[/versions/<version>]`; the version defaults to `latest`.

### Azure Key Vault

| Setting | Description |
|---|---|
| Vault URL | **Required.** The `https://` URL of the key vault, for example `https://my-vault.vault.azure.net`. It must be a key vault of an Azure cloud (`*.vault.azure.net`, or `*.vault.azure.cn`, `*.vault.usgovcloudapi.net`, `*.vault.microsoftazure.de`): the platform sends its identity's token to it. A vault on another host (Azure Stack Hub) is [declared in the environment](#advanced-declare-a-vault-in-the-environment). |
| Sign-in method | **Managed or workload identity** (recommended: AKS Workload Identity, an App Service or Container Apps identity, a VM's managed identity; nothing stored; the client ID and tenant ID are optional), or **Client secret** (an application's tenant ID, client ID and client secret, stored encrypted). |

The identity needs the **Key Vault Secrets User** role (or `get` and `list` secret permissions). A path is `<secret name>` or `<secret name>/<version>`.

### 1Password

| Setting | Description |
|---|---|
| Address | **Required.** The `https://` address of your 1Password Connect server, for example `https://connect.example.com:8080`. The server the [1Password integration](#1password-the-vault-and-the-integration) uses works too. |
| Connect server access token | **Required.** An access token of that Connect server, stored encrypted. Grant it read access to the vaults people will pick from. |
| CA certificate (PEM) | Paste the CA that signs the Connect server certificate, for a private CA. |

The picker lists your 1Password vaults as folders, then their items by title. A path is `<vault id>/<item id>`: the picker writes ids, so renaming a vault or an item in 1Password keeps every reference working (a name without spaces, typed by hand, resolves too). The **key** is the field to read, shown by its label (`username`, `password`, `credential`, a custom field, the item's one-time password); the reference stores the field's id. A one-time password field reads as its seed (the `otpauth://` URI): the platform computes the codes itself, which is what a [browser profile](../usage/resources/browser-profiles.md) needs to answer an MFA prompt unattended. Grant path prefixes for 1Password are vault ids, shown next to each vault in the picker.

Only a Connect server is supported (not a service account token), as for the integration.

#### 1Password: the vault and the integration

1Password appears in two places, for two needs, and both read through the same 1Password Connect client of the platform:

- **The secret vault** (this page): any secret field and any browser profile reads an item's field instead of holding a copy. No agent sees the value.
- **The [1Password integration](../usage/resources/integrations.md)** (Integrations): gives agents tools to search items, read a one-time password code, create or update items, audit sign-ins (Events API) and provision users (SCIM bridge).

Connect the vault for secrets the platform itself uses; add the integration for what agents do in 1Password. They can share the Connect server, each with its own token.

## Grant a vault

Each vault is used by:

- **Administrators only** (the default). Administrators always have every path of every enabled vault.
- **Everyone**, optionally restricted to path prefixes: `secret/shared, secret/ci`.
- **Specific groups and users**: add a group or a user, and the path prefixes each may use (empty: every path).

Prefixes match whole path segments and are case-sensitive: `secret/soc` admits `secret/soc/github`, never `secret/soc-staging/github`.

**Browsable** decides whether people who are not administrators may list secret names in the picker (names can be sensitive). Without it, they choose the vault and type the path, still checked against their grant.

The grants are enforced by the platform, never by the screen alone:

- A user who is granted no path of a vault does not see it in the picker.
- A reference to a path outside the user's grant is refused when it is saved (`403`, naming the field), in every picker request, and when it is checked.
- An integration, an MCP server or a browser profile holding a reference its editor could not have written (an administrator stored it) may still be renamed, tagged, shared or given a new secret, but an edit that changes where it sends the secret (a base URL, an MCP server's URL or command, a browser profile's start URL, sign-in URL or private hosts) is refused with a `403`, unless the same edit replaces that field.
- The same rules apply to imports and to the agent tools (the agent builder, `save_browser_profile`).

!!! warning "Removing a grant does not revoke the references already stored"
    A grant is checked when a reference is saved, when an entity holding one is re-pointed, in the picker and when a reference is checked, not each time the platform reads the secret. Removing someone's grant stops them from storing new references and from re-pointing the entities that hold them, but the references they stored while they were granted keep working. To stop them, disable the vault (every read of it fails at once), or replace those fields: the vault's secret field count, in the list, shows which fields read from it.

!!! warning "Grant everything only to a vault scoped to what everyone may read"
    A vault granted to everyone with no path prefix lets anyone who can edit an integration point it at their own server and have the platform send it any secret the vault identity can read. Give the grant path prefixes, or grant the vault to groups. The security audit reports a vault open to everyone with no path restriction.

## Pick a secret

Every secret field the platform stores has a **From a vault** button, shown when you may use at least one vault:

| Where | Fields |
|---|---|
| Integrations | API key, bot token, client secret (an OpenAPI integration's API key, bearer token and client secret included) |
| MCP servers | Each header value, each environment variable value, the OAuth client secret |
| LLM providers (Settings > AI Models) | API key, the AI gateway token |
| Variables | The value of a secret variable |
| Channel bots (Settings > Channels & Bots) | Bot token, client secret, each value of the extra configuration (signing secret, verify token, app secret, public key, webhook secret token) |
| Browser profiles (Tools & Integrations) | Username, password, TOTP secret |
| Settings > Access and security > Authentication | The OpenID Connect client secret |
| Settings > Observability | The Langfuse public and secret keys |

The picker opens on the vaults you may use. Browse the folders, search, or reuse a path from **Recent**; pick the secret, then the **key** to read when the secret holds several entries (a KV secret, a JSON secret), or **Whole value**. **Check** reads the secret on the server and says whether it can be read, never what it is. **Use this secret** puts the reference in the field. When the vault does not let you list it, the picker says so and you type the path.

A vault answers its names a page at a time (25 for Azure Key Vault, up to 100 for the others). While more names remain, the picker says how many are loaded and that the search covers those only; **Load more** loads the next page, and a search loads the next pages by itself, up to 10 pages in all. The keys of a path you type are read once you leave the field, press Enter or **Check**, never while you type.

A field holding a reference shows it as a chip: the vault's display name, the path and the key, with **Change** (type a value or pick another secret) and, where the field can be emptied, **Clear**. A path you are not granted shows as hidden. A reference you type or paste in the field becomes a chip once you leave the field. References typed by hand before this picker existed are shown the same way. **Paste a reference instead**, in the picker, still accepts one written by hand:

```
vault://<reference name>/<path>#<key>
```

The reference is the whole value of the field: `Bearer vault://corp/...` in a header is stored as text, not read from the vault. Store `Bearer <token>` in the vault instead. An MCP server reads only what its transport sends: the environment variables for a local (`stdio`) server, the headers for a remote one.

The `vault://` prefix is reserved in these fields: a value starting with it is always read as a reference. A value that starts with it but is not a valid reference is listed by the security audit and flagged on its entity page. Any other field, a client ID or a URL for example, refuses a reference.

## Use a secret in a prompt

A prompt, a persona, an assignment, a flow node or a custom tool never holds a reference: a secret reaches them through a **secret variable**. The **Variable** button next to these editors lists the variables you may use, secret ones badged, and inserts the placeholder. **Create a secret variable from a vault secret** opens the picker, creates a secret variable holding the reference (only you see it until you share it) and inserts it.

The value of a secret variable reaches the model call only: what the run stores, traces and logs holds a marker instead. See [Variables](../usage/resources/variables.md#where-a-secret-value-goes).

## Cache and rotation

A secret read from a vault is kept in the process memory for the vault's **cache duration** (Settings > Access and security > Secret vaults; `SECRET_VAULT_CACHE_SECONDS`, 300 by default, when empty), then dropped from memory whether or not it is used again, and read again on its next use. A secret rotated in the vault is therefore used within that time, without a restart. `0` reads the vault on every use.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_VAULT_CACHE_SECONDS` | No | `300` | How long a secret read from a vault stays in memory, for a vault with no cache duration of its own. `0` reads it on every use. |

- When an integration's service refuses its credential (`401` or `403`), the integration's references are dropped from the cache and read again on the next call. So is a client secret refused by an OAuth token endpoint (a token refresh) or by an A2A partner's token endpoint. A token refresh refused that way (`invalid_client`, or GitHub's and Slack's client refusals inside a `200`) is tried once more with the secret read again when the vault now holds another value, before the integration is marked as needing re-authorization. When the vault cannot be read again, the refresh backs off and tries later instead.
- An LLM provider or a remote MCP server refusing its key does not drop it from the cache: the rotated key is used once the cache time has passed.
- An LLM provider whose API key is a reference switches to a rotated key within the same time, in a failover chain too: its client is rebuilt as soon as the key read from the vault changes.
- The Discord bot keeps one connection open with the token it connected with. When Discord ends it (you reset the token, which revokes the old one), the bot connects again 30 seconds later with the token read from the vault again, not from memory. Consecutive failures wait longer, up to 10 minutes.
- Concurrent uses of one secret share a single vault request.
- A failed read is not retried for 15 seconds, so a broken reference does not hammer the vault. Nor is a failed login or token request (HashiCorp AppRole or Kubernetes, Azure, Google Cloud): the reads waiting for it fail at once instead of each trying again.
- Every request to a vault times out after 10 seconds, the requests that obtain the platform's own vault credentials included. The AWS SDK retries a failed call once.
- The login page waits at most 3 seconds for the OpenID Connect client secret. Past that, OpenID Connect sign-in is off for that request (local sign-in and SAML are not affected); the read goes on, and the next login uses its result while the vault's cache duration keeps it.
- A change to a vault in Settings applies on every replica and worker within 5 seconds. A vault whose sign-in settings change is signed in again and its cached secrets dropped; a change to its grants alone keeps them.
- Loading the variables of a run reads at most 8 secret variables from vaults at a time.

## When a secret cannot be read

The feature that needs the secret fails with a generic error. An API call, a connection test for example, answers `502` with "A secret stored in a vault could not be read. An administrator can find the details in the server logs." In an agent run, an MCP server whose secret cannot be read is left out, and the other servers still load. The vault's answer never reaches the user.

- The **server log** holds the details: the vault, the path and the vault's reason (an HTTP status, an AWS error code), never the value.
- The **entity page** (an integration, an MCP server, a variable, an LLM provider, a channel bot) shows which fields are read from a vault and, for a field whose last read failed, when and why. A field holding a value that starts with `vault://` but is not a valid reference is flagged as such.
- The **vault's status** in Settings > Access and security > Secret vaults shows its last connection test, with the reason.
- The **security audit** (Settings > Access and security) reports the failures of the last 7 days.

A failure is kept for 7 days in Redis, and cleared by the next successful read of that field. It is shown only while the field still holds the reference that failed. The record holds the reason, the vault and a keyed fingerprint of the reference, never its path.

## What stays secret

- A credential read from a vault lives in the platform's memory only. It is never written to the database, to Redis, to a log or to a trace.
- What opens a vault (its token, key or client secret) is stored encrypted and never returned by an API, logged or traced.
- The picker lists secret names and key names, never a value. A check answers OK or why not.
- An API response never carries a secret. It says which fields are read from which vault, and gives the path and key of a reference only to someone who may edit the entity and use that path.
- An integration export with credentials carries the reference as written, never the value. A duplicate, an import and the Filigran AI Catalog keep the reference as it is and never read the vault.
- The reference itself is stored encrypted, like any secret.

## Network and TLS

The platform never follows a redirect from a vault, and always verifies its TLS certificate. Paste your CA in the vault's **CA certificate** when HashiCorp Vault uses a private CA; there is no setting that turns verification off.

A vault connected in **Settings** is reached on a private network (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, IPv6 unique local addresses) or a public address, never on a loopback, link-local, unspecified, reserved or multicast address, nor on a cloud metadata address such as `169.254.169.254`: the platform would otherwise send requests to them from its own network position. This applies to the HashiCorp and 1Password **Address** and the AWS **VPC endpoint URL**. An Azure **Vault URL** must be a key vault of an Azure cloud ([Azure Key Vault](#azure-key-vault)), and Google Cloud is always reached at Google's own API and token endpoint, whatever the `token_uri` of a pasted service account key says: neither can name an address on your network. The address is checked when it is saved (a new vault, or an edit that changes it) and before each connection test (a `422` with one generic message; the address and the reason go to the backend log only), with every address its name resolves to; a name that does not resolve yet is accepted. Every connection to a HashiCorp or 1Password vault is checked again on the address it actually opens, with no second DNS lookup. The AWS SDK resolves and connects itself, so a VPC endpoint is checked again each time the platform builds its Secrets Manager client, not on each connection. Behind an outbound proxy (`HTTP_PROXY` / `HTTPS_PROXY`, `NO_PROXY` applied), the proxy connects, so those connections are checked on the addresses the name resolves to from the platform, before the request is sent through the proxy. Vaults declared in the environment are the operator's and are not checked.

A vault on the platform's own host (a loopback address) is allowed when its origin is listed in `SECRET_VAULT_TRUSTED_ORIGINS`. It is an environment variable only, so an administrator session cannot widen it, and the security controls report a list that is not empty (`network.secret_vault_trusted_origins`). Link-local and cloud metadata addresses stay refused for a listed origin.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_VAULT_TRUSTED_ORIGINS` | No | — | Comma-separated origins (`scheme://host[:port]`, for example `http://127.0.0.1:8200`) that a vault connected in Settings may reach on a loopback address. Matched on the origin: `http://127.0.0.1:8200` covers that service and nothing on another port, scheme or host. A malformed entry stops the platform at startup. |

A HashiCorp address or an AWS endpoint may be a plain `http://` URL (a Vault Agent on the same host, a local test). There is no TLS then: off loopback, the security audit reports it (`encryption.vault_provider_plain_http`).

## Advanced: declare a vault in the environment

A vault can also be declared in the platform's environment, for a deployment managed as code. It works like a vault connected in Settings and is listed there read-only, as **Declared in the environment**, with a lock: change it in the environment and restart.

```bash
SECRET_VAULT_<NAME>_TYPE=hashicorp   # or aws, gcp, azure
SECRET_VAULT_<NAME>_<SETTING>=...
```

`<NAME>` is the reference name, in lowercase: `SECRET_VAULT_CORP_TYPE` declares `vault://corp/...`. Set the same variables on the platform and the worker containers: both read secrets, and both log at startup the vaults they declare and the declarations they refuse. A declaration takes precedence over a vault of the same name connected in Settings, which the security audit then reports (`encryption.vault_provider_shadowed`).

Its credentials come from the environment or the workload identity: a token file, an AppRole, a Kubernetes service account, an IAM role, GKE Workload Identity, an Azure managed identity. Prefer a `_FILE` setting (`TOKEN_FILE`, `SECRET_ID_FILE`) or a workload identity to an inline `TOKEN` or `SECRET_ID`: a value in the environment is visible to whoever can inspect the container. A declaration the platform cannot use does not stop the platform: every secret read from it fails, and the security audit names the variable to fix.

| Type | Settings |
|---|---|
| `hashicorp` | `ADDRESS` (required), `NAMESPACE`, `AUTH_METHOD` (`token`, `approle`, `kubernetes`), `TOKEN` / `TOKEN_FILE`, `ROLE_ID`, `SECRET_ID` / `SECRET_ID_FILE`, `ROLE`, `JWT_FILE`, `AUTH_MOUNT`, `KV_VERSION`, `KV_MOUNT`, `CA_CERT_FILE` |
| `aws` | `REGION`, `ENDPOINT_URL` (the SDK's default credential chain) |
| `gcp` | `PROJECT` (Application Default Credentials) |
| `azure` | `VAULT_URL` (required), `CLIENT_ID`, `TENANT_ID` (a managed or workload identity) |
| `onepassword` | `ADDRESS` (required, the 1Password Connect server), `TOKEN` / `TOKEN_FILE` (a Connect access token), `CA_CERT_FILE` |

Who may use a declared vault is two settings, read for every type, and shown read-only in Settings:

| Setting | Description |
|---|---|
| `WRITERS` | `admins` (default): only administrators may store a reference to it. `all`: anyone who may edit a secret field may store one. |
| `PATHS` | Comma-separated path prefixes that references to it may read, for example `secret/xtm-one,secret/shared`, matched segment by segment. Empty (default): every path. Checked when a reference is saved (`422`) and again when it is read, so narrowing `PATHS` later stops the references already stored outside it. |

```bash
SECRET_VAULT_CORP_TYPE=hashicorp
SECRET_VAULT_CORP_ADDRESS=https://vault.example.com:8200
SECRET_VAULT_CORP_AUTH_METHOD=token
SECRET_VAULT_CORP_TOKEN_FILE=/run/secrets/vault-token
SECRET_VAULT_CORP_WRITERS=admins
SECRET_VAULT_CORP_PATHS=secret/xtm-one
```

## Security audit findings

The security audit (Settings > Access and security) reports, in the **Encryption** category, once a vault exists or a secret field holds a value starting with `vault://`:

| Finding | Severity | Meaning |
|---|---|---|
| `encryption.vault_provider_refused` | Warning | A vault cannot be used: a declaration or a connected vault whose settings are refused. The finding names what to fix. |
| `encryption.vault_provider_shadowed` | Warning | A vault connected in Settings has the name of one declared in the environment, which takes precedence: its settings and grants have no effect. |
| `encryption.vault_provider_failing` | Warning | A connected vault failed its last connection test. |
| `encryption.vault_provider_plain_http` | Warning | A vault's address is plain HTTP outside loopback: the secrets and the platform's token cross the network unencrypted. |
| `encryption.vault_provider_open_writers` | Warning | A vault is open to everyone with no path restriction (`WRITERS=all` with no `PATHS`, or a grant to everyone with no path prefix). |
| `encryption.vault_provider_static_key` | Info | A connected vault signs in with a long-lived cloud key stored in the platform (an AWS access key, a Google service account key, an Azure client secret) instead of a workload identity. |
| `encryption.vault_provider_unused` | Info | A connected, enabled vault that no secret field reads from. |
| `encryption.vault_reference_unknown_provider` | Warning | A stored secret references a vault that does not exist. |
| `encryption.vault_reference_invalid` | Warning | A secret field holds a value starting with `vault://` that is not a valid reference, so every use of it fails. The finding names the entity and the field, never the value. |
| `encryption.vault_reference_failing` | Warning | Secrets that still hold their reference could not be read from their vault in the last 7 days. |
| `encryption.vault_failures_unknown` | Info | Redis did not answer, so the recent failures could not be checked. No pass finding is reported then. |
| `encryption.vault_references_resolving` | Pass | Every stored reference names an existing vault and none failed lately; the values live in memory only. |

See [Configuration](configuration.md#security) for the other security settings.
