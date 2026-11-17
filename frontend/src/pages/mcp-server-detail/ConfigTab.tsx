/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { useEffect, useRef, useState } from "react";
import {
  Wrench,
  RefreshCw,
  XCircle,
  TestTube2,
  Lock,
  ExternalLink,
  CheckCircle2,
  AlertCircle,
  Eye,
  EyeOff,
  Search,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SettingsField, SettingsSwitchField, focusFirstInvalidField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { ToolTestDialog } from "@/components/ui/tool-test-dialog";
import { KeyValueEditor } from "@/components/ui/key-value-editor";
import { SecretField } from "@/components/ui/secret-field";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { RelayRouteField } from "@/components/ui/relay-route-field";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { pickSharingChange } from "@/lib/credential-exposure";
import { useUpdateMCPServer, useRefreshMCPTools, useTestMCPTool, useAuthorizeMCPServer } from "@/hooks/api/useMCPServerApi";
import { TRANSPORT_DISPLAY_NAMES } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { MCPServer, MCPToolInfo } from "@/types";
import { argsFieldText, editedArgs } from "./edited-args";
import { AuthModePicker, TransportPicker } from "./ConnectionPickers";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Configuration Card (editable by admins for company-managed, creator otherwise)
// ---------------------------------------------------------------------------

function ConfigurationCard({ srv, canManage }: { srv: MCPServer; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateMCPServer();
  const { toast } = useToast();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const [editing, setEditing] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);

  const [editName, setEditName] = useState(srv.name);
  const [nameError, setNameError] = useState(false);
  const [urlError, setUrlError] = useState(false);
  const [editTransport, setEditTransport] = useState<"stdio" | "sse" | "streamable_http">(srv.transport);
  const [editCommand, setEditCommand] = useState(srv.command || "");
  const [editArgs, setEditArgs] = useState(argsFieldText(srv.args));
  const [editUrl, setEditUrl] = useState(srv.url || "");
  const [editTags, setEditTags] = useState<string[]>(srv.tags || []);
  const [editEnvPairs, setEditEnvPairs] = useState<{ key: string; value: string }[]>([{ key: "", value: "" }]);
  const [editHeaderPairs, setEditHeaderPairs] = useState<{ key: string; value: string }[]>([{ key: "", value: "" }]);
  const [editAuthMode, setEditAuthMode] = useState<"headers" | "oauth">(srv.auth_mode);
  const [editAuthorizeUrl, setEditAuthorizeUrl] = useState(srv.authorize_url || "");
  const [editTokenUrl, setEditTokenUrl] = useState(srv.token_url || "");
  const [editRegistrationUrl, setEditRegistrationUrl] = useState(srv.registration_url || "");
  const [editScopes, setEditScopes] = useState(srv.scopes || "");
  const [editClientId, setEditClientId] = useState("");
  const [editClientSecret, setEditClientSecret] = useState("");
  const [editHideFromChat, setEditHideFromChat] = useState(srv.hide_from_chat);
  const [editCompanyManaged, setEditCompanyManaged] = useState(srv.is_company_managed);
  const [editPublicCatalog, setEditPublicCatalog] = useState(srv.is_public_catalog ?? false);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(srv.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(srv.shared_group_permissions ?? null);
  const [editRelayGroupId, setEditRelayGroupId] = useState<string | null>(srv.relay_group_id ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(srv);

  const startEditing = () => {
    setEditing(true);
    setNameError(false);
    setUrlError(false);
    setEditRelayGroupId(srv.relay_group_id ?? null);
    setEditName(srv.name);
    setEditTransport(srv.transport as "stdio" | "sse" | "streamable_http");
    setEditCommand(srv.command || "");
    setEditArgs(argsFieldText(srv.args));
    setEditUrl(srv.url || "");
    setEditEnvPairs([{ key: "", value: "" }]);
    setEditHeaderPairs([{ key: "", value: "" }]);
    setEditAuthMode(srv.auth_mode);
    setEditAuthorizeUrl(srv.authorize_url || "");
    setEditTokenUrl(srv.token_url || "");
    setEditRegistrationUrl(srv.registration_url || "");
    setEditScopes(srv.scopes || "");
    // Credentials are write-only — never echoed back by the API, so the inputs
    // start blank and an untouched field leaves the stored value alone.
    setEditClientId("");
    setEditClientSecret("");
    setEditHideFromChat(srv.hide_from_chat);
    setEditCompanyManaged(srv.is_company_managed);
    setEditPublicCatalog(srv.is_public_catalog ?? false);
    setEditSharedGroupIds(srv.shared_group_ids ?? null);
    setEditSharedGroupPermissions(srv.shared_group_permissions ?? null);
    shareGrants.reset();
    setShowSecrets(false);
  };

  const isRemote = editTransport !== "stdio";
  // Derived from the *edit* state, not the saved row: switching the mode has to
  // reveal the OAuth block in the same pass that saves it, otherwise the
  // selector below would appear to do nothing until after a round-trip.
  const isOAuth = editAuthMode === "oauth" && isRemote;
  //  Switching away from OAuth makes the backend discard the stored tokens
  //  (mcp_servers.py), so the connection has to be re-authorized afterwards.
  const willDropTokens =
    srv.auth_mode === "oauth" && editAuthMode === "headers" && srv.oauth_status === "connected";

  const handleSave = async () => {
    // Say what is missing instead of a disabled Save, as the create form does:
    // the error under the field, the field marked invalid and focused.
    const missingName = !editName.trim();
    const missingUrl = isRemote && !editUrl.trim();
    setNameError(missingName);
    setUrlError(missingUrl);
    if (missingName || missingUrl) {
      focusFirstInvalidField([missingName && "edit-server-name", missingUrl && "edit-server-url"]);
      return;
    }
    const env: Record<string, string> = {};
    for (const pair of editEnvPairs) {
      if (pair.key.trim() && pair.value.trim()) env[pair.key.trim()] = pair.value.trim();
    }
    const headers: Record<string, string> = {};
    for (const pair of editHeaderPairs) {
      if (pair.key.trim() && pair.value.trim()) headers[pair.key.trim()] = pair.value.trim();
    }

    const resolvedGroupIds = editCompanyManaged ? null : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : null);
    const resolvedGroupPermissions = editCompanyManaged ? null : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);
    const payload: { id: string; name?: string; tags?: string[]; transport?: "stdio" | "sse" | "streamable_http"; command?: string; args?: string[]; env?: Record<string, string>; url?: string; headers?: Record<string, string>; auth_mode?: "headers" | "oauth"; authorize_url?: string; token_url?: string; registration_url?: string; scopes?: string; client_id?: string; client_secret?: string; hide_from_chat?: boolean; is_company_managed?: boolean; is_public_catalog?: boolean; shared_group_ids?: string[] | null; shared_group_permissions?: Record<string, string> | null } = {
      id: srv.id,
      name: editName.trim(),
      tags: editTags,
      transport: editTransport,
      hide_from_chat: editHideFromChat,
      is_company_managed: editCompanyManaged,
      is_public_catalog: editPublicCatalog,
      shared_group_ids: resolvedGroupIds,
      shared_group_permissions: resolvedGroupPermissions,
      ...shareGrants.toPayload(editCompanyManaged),
    };
    if (editTransport === "stdio") {
      if (editCommand.trim()) payload.command = editCommand.trim();
      // The arguments are read masked: only an edited field is sent, and the
      // backend puts each masked value it still holds back.
      const args = editedArgs(editArgs, srv.args);
      if (args) payload.args = args;
    } else {
      if (editUrl.trim()) payload.url = editUrl.trim();
    }
    if (Object.keys(env).length > 0) payload.env = env;
    if (Object.keys(headers).length > 0) payload.headers = headers;
    // Only meaningful for remote transports; stdio has no auth mode at all.
    if (isRemote) payload.auth_mode = editAuthMode;
    // A stdio server runs on the platform itself: no relay can carry it.
    const relayGroupId = isRemote ? editRelayGroupId : null;
    if (relayGroupId !== (srv.relay_group_id ?? null)) {
      (payload as Record<string, unknown>).relay_group_id = relayGroupId;
    }
    if (isOAuth) {
      // URLs round-trip through the read view, so send them as edited —
      // including cleared, which is how you undo a wrong endpoint.
      payload.authorize_url = editAuthorizeUrl.trim();
      payload.token_url = editTokenUrl.trim();
      payload.registration_url = editRegistrationUrl.trim();
      payload.scopes = editScopes.trim();
      // Credentials are only sent when typed: a blank field means "leave the
      // stored value alone", not "erase it".
      if (editClientId.trim()) payload.client_id = editClientId.trim();
      if (editClientSecret.trim()) payload.client_secret = editClientSecret.trim();
    }

    // Lending this server (and what it stores) to new people, or taking it
    // back from people who use it, is said before it is saved.
    if (!(await confirmSharing({ entity_type: "mcp_server", entity_id: srv.id, change: pickSharingChange(payload) }))) return;
    update.mutate(payload, {
      onSuccess: () => {
        toast({ title: t_i18n("Server updated"), description: t_i18n("Configuration saved successfully."), variant: "success" });
        setEditing(false);
      },
    });
  };

  return (
    <>
      {consentElement}
    <EntityConfigCard
      entityLabel="server"
      isCompanyManaged={srv.is_company_managed}
      userId={srv.user_id}
      sharedGroupIds={srv.shared_group_ids}
      ownerName={srv.owner_name}
      canManage={canManage}
      editing={editing}
      onStartEditing={startEditing}
      onCancel={() => setEditing(false)}
      onSave={handleSave}
      isSaving={update.isPending}
      editCompanyManaged={editCompanyManaged}
      onCompanyManagedChange={setEditCompanyManaged}
      editPublicCatalog={editPublicCatalog}
      onPublicCatalogChange={setEditPublicCatalog}
      editSharedGroupIds={editSharedGroupIds}
      onSharedGroupIdsChange={setEditSharedGroupIds}
      editSharedGroupPermissions={editSharedGroupPermissions}
      onSharedGroupPermissionsChange={setEditSharedGroupPermissions}
      userSharing={shareGrants.props}
      onTransferOwner={async (userId) => {
        if (!(await confirmSharing({ entity_type: "mcp_server", entity_id: srv.id, change: { new_owner_id: userId } }, { confirmLabel: t_i18n("Transfer") }))) return;
        update.mutate(
          { id: srv.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        );
      }}
      editHeaderExtra={
        <Button
          variant="ghost" size="sm" className="h-6 px-2 content-compact"
          onClick={() => setShowSecrets(!showSecrets)}
        >
          {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
          {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
        </Button>
      }
      readView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
              <p className="content-base font-medium">{srv.name}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Transport")}</div>
              <p className="content-base">{t_i18n(TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport)}</p>
            </div>
          </div>
          {srv.transport === "stdio" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Command")}</div>
                <p className="content-base font-mono-plex">{srv.command || "—"}</p>
              </div>
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Arguments")}</div>
                <p className="content-base font-mono-plex truncate">{argsFieldText(srv.args) || "—"}</p>
              </div>
            </div>
          )}
          {(srv.transport === "sse" || srv.transport === "streamable_http") && (
            <>
              <div>
                <div className="content-compact text-default-secondary mb-1">{srv.transport === "streamable_http" ? t_i18n("Endpoint URL") : t_i18n("SSE URL")}</div>
                <p className="content-base font-mono-plex break-all">{srv.url || "—"}</p>
              </div>
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Authentication")}</div>
                <p className="content-base">{srv.auth_mode === "oauth" ? "OAuth 2.0" : t_i18n("Secret Headers")}</p>
              </div>
              <RelayRouteField readOnly value={srv.relay_group_id} onChange={() => undefined} destination={srv.url} />
              {srv.auth_mode === "oauth" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <div className="content-compact text-default-secondary mb-1">{t_i18n("Authorize URL")}</div>
                    <p className="content-base font-mono-plex break-all truncate">{srv.authorize_url || "—"}</p>
                  </div>
                  <div>
                    <div className="content-compact text-default-secondary mb-1">{t_i18n("Token URL")}</div>
                    <p className="content-base font-mono-plex break-all truncate">{srv.token_url || "—"}</p>
                  </div>
                  {srv.scopes && (
                    <div className="sm:col-span-2">
                      <div className="content-compact text-default-secondary mb-1">{t_i18n("Scopes")}</div>
                      <p className="content-base font-mono-plex">{srv.scopes}</p>
                    </div>
                  )}
                  <div className="sm:col-span-2">
                    <div className="content-compact text-default-secondary mb-1">{t_i18n("OAuth Client")}</div>
                    <p className="content-base">
                      {srv.dcr_registered
                        ? t_i18n("Registered automatically with the server")
                        : srv.has_client_id
                          ? t_i18n("Configured manually")
                          : t_i18n("Not registered yet: registers on first authorization")}
                    </p>
                  </div>
                </div>
              )}
            </>
          )}
        </>
      }
      editView={
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField
              id="edit-server-name"
              label={t_i18n("Server Name")}
              required
              helper={t_i18n("Shown in lists and to agents that use its tools.")}
              error={nameError ? t_i18n("Enter a name for this server.") : undefined}
            >
              <Input id="edit-server-name" aria-labelledby={settingsFieldLabelId("edit-server-name")} aria-describedby={settingsFieldHintId("edit-server-name")} aria-required aria-invalid={nameError || undefined} value={editName} onChange={(e) => { setEditName(e.target.value); if (nameError) setNameError(false); }} />
            </SettingsField>
            <SettingsField id="edit-server-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="edit-server-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("edit-server-tags")} value={editTags} onChange={setEditTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
            <TransportPicker value={editTransport} onChange={(next) => { setEditTransport(next); if (urlError) setUrlError(false); }} />
          </div>

          {editTransport === "stdio" ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <SettingsField
                id="edit-server-command"
                label={t_i18n("Command")}
                helper={t_i18n("The program that starts the server, for example npx, uvx or docker.")}
                info={t_i18n("Runs as a subprocess of the platform, so it must be installed where the platform runs. Without a command the server cannot start.")}
              >
                <Input id="edit-server-command" aria-labelledby={settingsFieldLabelId("edit-server-command")} aria-describedby={settingsFieldHintId("edit-server-command")} value={editCommand} onChange={(e) => setEditCommand(e.target.value)} placeholder="npx" className="font-mono-plex" />
              </SettingsField>
              <SettingsField
                id="edit-server-args"
                label={t_i18n("Arguments (space-separated)")}
                helper={t_i18n("Passed to the command in this order, separated by spaces.")}
                info={t_i18n("For example -y @modelcontextprotocol/server-github. Replace any placeholder path with a real path before creating the server.")}
              >
                <Input id="edit-server-args" aria-labelledby={settingsFieldLabelId("edit-server-args")} aria-describedby={settingsFieldHintId("edit-server-args")} value={editArgs} onChange={(e) => setEditArgs(e.target.value)} placeholder="-y @modelcontextprotocol/server-github" className="font-mono-plex" />
              </SettingsField>
            </div>
          ) : (
            <SettingsField
              id="edit-server-url"
              required
              label={editTransport === "streamable_http" ? t_i18n("Server URL") : t_i18n("SSE URL")}
              helper={t_i18n("The address this platform connects to.")}
              info={t_i18n("The full URL of the MCP endpoint, for example https://mcp.example.com/mcp. Also used to detect OAuth settings.")}
              error={urlError ? t_i18n("Enter the URL this platform connects to.") : undefined}
            >
              <Input id="edit-server-url" aria-labelledby={settingsFieldLabelId("edit-server-url")} aria-describedby={settingsFieldHintId("edit-server-url")} aria-required aria-invalid={urlError || undefined} value={editUrl} onChange={(e) => { setEditUrl(e.target.value); if (urlError) setUrlError(false); }} placeholder={editTransport === "streamable_http" ? "https://mcp.example.com/mcp" : "https://mcp.example.com/sse"} className="font-mono-plex" />
            </SettingsField>
          )}

          <RelayRouteField
            value={isRemote ? editRelayGroupId : null}
            onChange={setEditRelayGroupId}
            destination={isRemote ? editUrl : null}
            unavailableReason={
              isRemote ? undefined : t_i18n("A stdio server runs on the platform itself: only a remote server can be routed through a relay group.")
            }
          />

          {/* Authentication mode — remote transports only, mirroring the
              create form. Without it the mode was read-only after creation, so
              a server added with header auth that turns out to need OAuth had
              to be deleted and recreated. */}
          {isRemote && (
            <AuthModePicker value={editAuthMode} onChange={setEditAuthMode}>
              {willDropTokens && (
                <p className="content-compact text-feedback-warning-primary pt-1">
                  {t_i18n("Switching to Secret Headers discards the stored OAuth tokens: you would have to authorize this server again.")}
                </p>
              )}
            </AuthModePicker>
          )}

          {/* Secrets section — env vars for stdio, headers for remote. Stored
              values never come back; the save sends the set only when an entry
              is present, replacing what is stored — hence the hint, shown only
              when something is stored.
              Hidden in OAuth mode: the token is injected as the Authorization
              header at runtime, so offering a header editor alongside it (the
              create form never does) invites a conflicting hand-written one. */}
          {isOAuth ? null : editTransport === "stdio" ? (
            <KeyValueEditor
              pairs={editEnvPairs}
              onChange={setEditEnvPairs}
              showSecrets={showSecrets}
              onToggleSecrets={setShowSecrets}
              label="Environment Variables"
              helper="Passed to the server's process when it starts; a value can come from a vault."
              info="The name and value of each variable the server reads, e.g. GITHUB_TOKEN. A row with an empty name or value is not saved. Values stay hidden until you choose Show."
              hint={srv.has_env ? "Stored: add entries to replace" : undefined}
              keyLabel="Variable Name"
              valueLabel="Value"
              keyPlaceholders={editEnvPairs.map(() => "ENV_VAR_NAME")}
              valuePlaceholders={editEnvPairs.map(() => "secret value")}
              addLabel="+ Add another variable"
              allowVault
            />
          ) : (
            <KeyValueEditor
              pairs={editHeaderPairs}
              onChange={setEditHeaderPairs}
              showSecrets={showSecrets}
              onToggleSecrets={setShowSecrets}
              label="HTTP Headers"
              helper="Sent with every request to the server; a value can come from a vault."
              info="The name and value of each header, e.g. Authorization with Bearer and the token. A row with an empty name or value is not saved. Values stay hidden until you choose Show."
              hint={srv.has_headers ? "Stored: add entries to replace" : undefined}
              keyLabel="Header Name"
              valueLabel="Header Value"
              keyPlaceholders={editHeaderPairs.map((_, i) => i === 0 ? "Authorization" : "Header-Name")}
              valuePlaceholders={editHeaderPairs.map((_, i) => i === 0 ? "Bearer ..." : "value")}
              addLabel="+ Add another header"
              allowVault
            />
          )}

          {/* OAuth endpoints + optional credentials. All of it is optional:
              with the fields blank, the platform discovers the authorization
              server and registers itself when the connection is authorized. */}
          {isOAuth && (
            <div className="space-y-3">
              <div className="content-compact font-medium text-default-secondary">{t_i18n("OAuth 2.0 Configuration")}</div>
              <div className="grid gap-4 sm:grid-cols-2">
                <SettingsField
                  id="edit-server-authorize-url"
                  label={t_i18n("Authorize URL")}
                  helper={t_i18n("Where you sign in to grant this platform access.")}
                  info={t_i18n("The OAuth authorization endpoint, for example https://provider.com/oauth/authorize. Empty: detected from the server URL when the server publishes OAuth metadata.")}
                >
                  <Input id="edit-server-authorize-url" aria-labelledby={settingsFieldLabelId("edit-server-authorize-url")} aria-describedby={settingsFieldHintId("edit-server-authorize-url")} value={editAuthorizeUrl} onChange={(e) => setEditAuthorizeUrl(e.target.value)} placeholder={t_i18n("Detected automatically")} className="font-mono-plex" />
                </SettingsField>
                <SettingsField
                  id="edit-server-token-url"
                  label={t_i18n("Token URL")}
                  helper={t_i18n("Where this platform exchanges the grant for an access token.")}
                  info={t_i18n("The OAuth token endpoint, for example https://provider.com/oauth/token. Empty: detected from the server URL when the server publishes OAuth metadata.")}
                >
                  <Input id="edit-server-token-url" aria-labelledby={settingsFieldLabelId("edit-server-token-url")} aria-describedby={settingsFieldHintId("edit-server-token-url")} value={editTokenUrl} onChange={(e) => setEditTokenUrl(e.target.value)} placeholder={t_i18n("Detected automatically")} className="font-mono-plex" />
                </SettingsField>
                <SettingsField
                  id="edit-server-registration-url"
                  label={t_i18n("Registration URL (optional)")}
                  helper={t_i18n("Where this platform registers itself as a client.")}
                  info={t_i18n("The dynamic client registration endpoint, for example https://provider.com/oauth/register. Empty: the one the server publishes, else the standard path.")}
                >
                  <Input id="edit-server-registration-url" aria-labelledby={settingsFieldLabelId("edit-server-registration-url")} aria-describedby={settingsFieldHintId("edit-server-registration-url")} value={editRegistrationUrl} onChange={(e) => setEditRegistrationUrl(e.target.value)} placeholder="https://provider.com/oauth/register" className="font-mono-plex" />
                </SettingsField>
                <SettingsField
                  id="edit-server-scopes"
                  label={t_i18n("Scopes (space-separated)")}
                  helper={t_i18n("The permissions asked for when you authorize the connection.")}
                  info={t_i18n("Separate scopes with a space, for example read write openid. Empty: the server's default scopes.")}
                >
                  <Input id="edit-server-scopes" aria-labelledby={settingsFieldLabelId("edit-server-scopes")} aria-describedby={settingsFieldHintId("edit-server-scopes")} value={editScopes} onChange={(e) => setEditScopes(e.target.value)} placeholder="read write" className="font-mono-plex" />
                </SettingsField>
                <SettingsField
                  id="edit-server-client-id"
                  label={t_i18n("Client ID (optional)")}
                  helper={t_i18n("The client this platform signs in as.")}
                  info={t_i18n("The client ID the provider gave you when you registered this platform. Empty: this platform registers itself automatically when you authorize the connection.")}
                >
                  <Input id="edit-server-client-id" aria-labelledby={settingsFieldLabelId("edit-server-client-id")} aria-describedby={settingsFieldHintId("edit-server-client-id")} type={showSecrets ? "text" : "password"} value={editClientId} onChange={(e) => setEditClientId(e.target.value)} placeholder={t_i18n("Leave empty to keep current")} className="font-mono-plex" />
                </SettingsField>
                <SettingsField
                  id="edit-server-client-secret"
                  label={t_i18n("Client Secret (optional)")}
                  helper={t_i18n("Only for a confidential client.")}
                  info={t_i18n("The secret paired with the client ID, for a confidential client. Empty: a public client, or the one registered automatically.")}
                >
                  <SecretField
                    id="edit-server-client-secret"
                    aria-label={t_i18n("Client Secret (optional)")}
                    aria-describedby={settingsFieldHintId("edit-server-client-secret")}
                    value={editClientSecret}
                    onChange={setEditClientSecret}
                    stored={srv.vault_secrets?.locations?.client_secret?.[0] ?? null}
                    hasStoredValue={Boolean(srv.has_client_secret)}
                  />
                </SettingsField>
              </div>
            </div>
          )}

          <SettingsSwitchField
            id="edit-server-hide-from-chat"
            label={t_i18n("Hide from chat")}
            helper={t_i18n("When enabled, this server won't appear in the chat tool picker or profile settings. It will still be available for agent configuration.")}
            checked={editHideFromChat}
            onCheckedChange={setEditHideFromChat}
          />
        </>
      }
    />
    </>
  );
}

// ---------------------------------------------------------------------------
// Discovered Tools Card — shows persisted tools, with refresh button
// ---------------------------------------------------------------------------

/** Refreshing and testing call the server with its credentials, which the
 *  API reserves to a manage right: a viewer without one sees the list only. */
function ToolsCard({ srv, canManage }: { srv: MCPServer; canManage: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const refreshTools = useRefreshMCPTools();
  const testTool = useTestMCPTool();
  const update = useUpdateMCPServer();
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [testingTool, setTestingTool] = useState<MCPToolInfo | null>(null);

  // Deep Investigation source designation. At most one tool per
  // server: the whole point is that a person picked THE tool an investigation
  // may call, and a list of them would be a list of things nobody chose
  // individually. Behind `DEEP_INVESTIGATION`: off, nothing is offered
  // or shown, and a stored designation is kept for when it comes back.
  const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");
  const designated = deepInvestigationEnabled ? (srv.investigation_tool_name ?? null) : null;
  const [argument, setArgument] = useState(srv.investigation_argument ?? "");
  useEffect(() => {
    setArgument(srv.investigation_argument ?? "");
  }, [srv.investigation_argument]);

  // Clicking another tool blurs the argument field first, so its write and the
  // designation's leave together. Sent in parallel they could land in either
  // order and pin the previous tool's argument on the new tool: each write
  // waits for the one before it.
  const designationWrites = useRef<Promise<unknown>>(Promise.resolve());
  const writeDesignation = (
    body: { investigation_tool_name?: string | null; investigation_argument: string | null },
  ) => {
    designationWrites.current = designationWrites.current
      .catch(() => undefined)
      .then(() => update.mutateAsync({ id: srv.id, ...body }))
      .catch(() => undefined);
  };

  const designate = (toolName: string | null) => {
    // Any change of tool clears the pinned argument: it named one of the
    // previous tool's parameters, and kept it would silently apply to the next.
    setArgument("");
    writeDesignation({ investigation_tool_name: toolName, investigation_argument: null });
  };

  const handleRefresh = () => {
    setRefreshError(null);
    refreshTools.mutate(srv.id, {
      onError: (err) => setRefreshError(err.message || t_i18n("Failed to connect to server")),
    });
  };

  const discoveredTools = srv.discovered_tools ?? [];
  const hasTools = discoveredTools.length > 0;

  return (
    <Card>
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <div className="flex items-center gap-2">
          <CardTitle className="content-base flex items-center gap-2">
            <Wrench className="h-4 w-4 text-accent" />
            {t_i18n("Discovered Tools")}
            {hasTools && (
              <Badge variant="cyan" className="content-caption ml-1">{t_i18n("{count} tools", { count: discoveredTools.length })}</Badge>
            )}
          </CardTitle>
          {srv.tools_discovered_at && (
            <span className="content-caption text-default-secondary">
              {t_i18n("Last refreshed {time}", { time: formatTimeAgo(intl, srv.tools_discovered_at) })}
            </span>
          )}
        </div>
        {canManage && (
          <Button
            variant="secondary"
            size="sm"
            className="h-7 gap-1.5 content-compact"
            onClick={handleRefresh}
            disabled={refreshTools.isPending} aria-busy={refreshTools.isPending || undefined}>
            {refreshTools.isPending && <Spinner size="sm" tone="inherit" />}{!refreshTools.isPending && <RefreshCw className="h-3 w-3" />}
            {refreshTools.isPending ? t_i18n("Refreshing...") : hasTools ? t_i18n("Refresh Tools") : t_i18n("Discover Tools")}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Loading state */}
        {refreshTools.isPending && (
          <div className="flex items-center justify-center py-6 gap-2">
            <Spinner size="md" tone="inherit" className="text-accent" />
            <p className="content-base text-default-secondary">{t_i18n("Connecting to server...")}</p>
          </div>
        )}

        {/* Error banner */}
        {refreshError && !refreshTools.isPending && (
          <div className="content-base px-4 py-3 rounded-sm flex items-center gap-2 bg-feedback-error-primary/10 text-feedback-error-primary">
            <XCircle className="h-4 w-4" />
            {refreshError}
          </div>
        )}

        {/* Empty state — no tools discovered yet */}
        {!hasTools && !refreshTools.isPending && (
          <div className="text-center py-6">
            <Wrench className="h-8 w-8 text-default-disabled mx-auto mb-2" />
            <p className="content-base text-default-secondary">{t_i18n("No tools discovered yet.")}</p>
            {canManage && (
              <p className="content-compact text-default-secondary mt-1">{t_i18n("Click \"Discover Tools\" to connect and list available tools from this MCP server.")}</p>
            )}
          </div>
        )}

        {/* Tool list — from persisted data */}
        {hasTools && !refreshTools.isPending && (
          <div className="space-y-1.5">
            {discoveredTools.map((tool) => (
              <div key={tool.name} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2 group">
                <code className="shrink-0 rounded-sm bg-accent/10 px-1.5 py-0.5 text-[11px] font-mono-plex text-accent">
                  {tool.name}
                </code>
                <span className="content-compact text-default-secondary leading-relaxed flex-1 min-w-0">{tool.description}</span>
                {canManage && deepInvestigationEnabled && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={update.isPending}
                    className={cn(
                      "h-6 gap-1 content-caption px-2 transition-opacity shrink-0",
                      // The designated tool stays visible; the others appear
                      // on hover, like Test. A designation is a standing
                      // permission, so it must be readable without hovering.
                      tool.name === designated
                        ? "text-accent"
                        : "opacity-0 group-hover:opacity-100",
                    )}
                    onClick={() =>
                      designate(tool.name === designated ? null : tool.name)
                    }
                  >
                    <Search className="h-3 w-3" />
                    {tool.name === designated
                      ? t_i18n("Investigation source")
                      : t_i18n("Use for investigation")}
                  </Button>
                )}
                {canManage && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 gap-1 content-caption px-2 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                    onClick={() => setTestingTool(tool)}
                  >
                    <TestTube2 className="h-3 w-3" />
                    {t_i18n("Test")}
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Designation detail. Present only when a tool is designated, so the
            server's ordinary configuration is not cluttered by a setting that
            applies to nothing. */}
        {designated && (
          <div className="rounded-sm border border-accent/30 bg-accent/5 px-3 py-2.5 space-y-2">
            <div className="flex items-center gap-2">
              <Search className="h-3.5 w-3.5 shrink-0 text-accent" />
              <span className="content-compact text-default-primary">
                {t_i18n(
                  "A Deep Investigation may call {tool} on this server. No other tool here is ever called.",
                  { tool: designated },
                )}
              </span>
            </div>
            <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center">
              <label
                htmlFor="investigation-argument"
                className="content-caption text-default-secondary sm:w-40 sm:shrink-0"
              >
                {t_i18n("Subject argument")}
              </label>
              <Input aria-label={t_i18n("Subject argument")}
                id="investigation-argument"
                value={argument}
                disabled={!canManage || update.isPending}
                placeholder={t_i18n("Read from the tool's schema")}
                className="h-7 content-compact"
                onChange={(e) => setArgument(e.target.value)}
                onBlur={() => {
                  const next = argument.trim() || null;
                  if (next !== (srv.investigation_argument ?? null)) {
                    writeDesignation({ investigation_argument: next });
                  }
                }}
              />
            </div>
            <p className="content-caption text-default-secondary">
              {t_i18n(
                "Left blank, the subject goes to the tool's single text parameter. Name it here when the tool takes several and the investigation cannot tell which.",
              )}
            </p>
          </div>
        )}
      </CardContent>

      {/* Tool test dialog */}
      <ToolTestDialog
        open={testingTool !== null}
        onOpenChange={(open) => { if (!open) setTestingTool(null); }}
        toolName={testingTool?.name ?? ""}
        toolDescription={testingTool?.description}
        inputSchema={testingTool?.input_schema}
        onRun={async (args) => {
          return new Promise((resolve) => {
            testTool.mutate(
              { serverId: srv.id, tool_name: testingTool?.name ?? "", arguments: args },
              {
                onSuccess: (data) => resolve(data),
                onError: (err) => resolve({ status: "error", message: err.message }),
              },
            );
          });
        }}
      />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// OAuth Status Card — shows connection status and authorize button
// ---------------------------------------------------------------------------

function OAuthStatusCard({ srv, canManage }: { srv: MCPServer; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const authorize = useAuthorizeMCPServer();
  const { toast } = useToast();

  // The backend resolves the endpoints and registers an OAuth client on the
  // fly when none is configured, so this button is the whole flow: no client
  // ID has to exist before it is pressed.
  const handleAuthorize = () => {
    authorize.mutate(srv.id, {
      onSuccess: (data) => {
        if (data.authorize_url) {
          window.location.href = data.authorize_url;
        }
      },
      onError: (err) => {
        toast({ title: t_i18n("OAuth failed"), description: err.message || t_i18n("Could not start authorization flow."), variant: "error" });
      },
    });
  };

  const isConnected = srv.oauth_status === "connected";
  const isError = srv.oauth_status === "error";

  // Never point at a button the viewer does not have: a reader who cannot
  // manage this server is told who can act instead of being sent to a
  // "button below" that is not rendered for them.
  const whoAuthorizes = srv.is_company_managed
    ? t_i18n("An administrator has to authorize this company-managed server.")
    : t_i18n("The owner of this server has to authorize it.");
  const statusDetail = isConnected
    ? t_i18n("The server is authenticated. Tokens are refreshed automatically.")
    : !canManage
      ? whoAuthorizes
      : isError
        ? t_i18n("Re-authorize to restore the connection.")
        : t_i18n("Click the button below to authorize this MCP server via OAuth.");

  return (
    <Card>
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <CardTitle className="content-base flex items-center gap-2">
          <Lock className="h-4 w-4 text-accent" />
          {t_i18n("OAuth Connection")}
        </CardTitle>
        <Badge
          variant={isConnected ? "success" : isError ? "destructive" : "outline"}
          className="content-caption"
        >
          {isConnected ? t_i18n("Connected") : isError ? t_i18n("Error") : t_i18n("Not connected")}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-3 rounded-sm bg-elevation-hover p-3">
          {isConnected ? (
            <CheckCircle2 className="h-5 w-5 text-feedback-success-primary shrink-0" />
          ) : isError ? (
            <AlertCircle className="h-5 w-5 text-feedback-error-primary shrink-0" />
          ) : (
            <AlertCircle className="h-5 w-5 text-default-secondary shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            <p className="content-base font-medium">
              {isConnected
                ? t_i18n("OAuth tokens are active")
                : isError
                  ? t_i18n("OAuth token refresh failed")
                  : t_i18n("Authorization required")}
            </p>
            <p className="content-compact text-default-secondary mt-0.5">{statusDetail}</p>
          </div>
        </div>

        {canManage && (
          <Button className="w-full gap-2" onClick={handleAuthorize} disabled={authorize.isPending}>
            <ExternalLink className="h-4 w-4" />
            {authorize.isPending
              ? t_i18n("Redirecting...")
              : isConnected
                ? t_i18n("Re-authorize")
                : t_i18n("Authorize")}
          </Button>
        )}

        {canManage && srv.is_company_managed && (
          <p className="content-compact text-default-secondary text-center">
            {t_i18n("This is a company-managed server: the connection you authorize is shared by every user of the platform.")}
          </p>
        )}

        {canManage && !srv.has_client_id && (
          <p className="content-compact text-default-secondary text-center">
            {t_i18n("No client credentials needed: this platform registers itself with the server when you authorize.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tab: Configuration
// ---------------------------------------------------------------------------

export function ConfigTab({ srv, canManage }: { srv: MCPServer; canManage: boolean }) {
  return (
    <div className="space-y-6">
      <ConfigurationCard srv={srv} canManage={canManage} />
      {srv.auth_mode === "oauth" && <OAuthStatusCard srv={srv} canManage={canManage} />}
      <ToolsCard srv={srv} canManage={canManage} />
    </div>
  );
}
