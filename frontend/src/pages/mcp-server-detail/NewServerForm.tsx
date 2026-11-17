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

import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ChevronLeft,
  Eye,
  EyeOff,
  Building2,
  Info,
  Search,
  TriangleAlert,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { KeyValueEditor } from "@/components/ui/key-value-editor";
import { SecretField } from "@/components/ui/secret-field";
import { RelayRouteField } from "@/components/ui/relay-route-field";
import { useAuth } from "@/hooks/useAuth";
import { useCreateMCPServer, useDiscoverMCPOAuth } from "@/hooks/api/useMCPServerApi";
import { useFormatter } from "@/i18n";
import { PresetPicker } from "./PresetCard";
import { AuthModePicker, TransportPicker } from "./ConnectionPickers";
import { findPlaceholderPath } from "./placeholder-args";
import { presetNeedsDockerCli, type MCPPreset } from "./presets";
import { argsFieldText, splitArgs } from "./edited-args";
import type { MCPOAuthDiscovery, MCPServer } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";

// ---------------------------------------------------------------------------
// New Server Form (shown when id === "new") — two-step: preset picker → config
// ---------------------------------------------------------------------------

export function NewServerForm() {
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const create = useCreateMCPServer();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  const discover = useDiscoverMCPOAuth();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const [showSecrets, setShowSecrets] = useState(false);

  // Step: "select" = pick a preset, "config" = fill in secrets & create
  const [step, setStep] = useState<"select" | "config">("select");
  const [selectedPreset, setSelectedPreset] = useState<MCPPreset | null>(null);

  // Form state
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [transport, setTransport] = useState<"stdio" | "sse" | "streamable_http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  // The placeholder path still in the arguments when Create was pressed.
  const [argsPlaceholder, setArgsPlaceholder] = useState<string | null>(null);
  const argsInputRef = useRef<HTMLInputElement>(null);
  const [url, setUrl] = useState("");
  const [envPairs, setEnvPairs] = useState<{ key: string; value: string }[]>([{ key: "", value: "" }]);
  const [headerPairs, setHeaderPairs] = useState<{ key: string; value: string }[]>([{ key: "", value: "" }]);
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);

  // OAuth state (for remote transports)
  const [authMode, setAuthMode] = useState<"headers" | "oauth">("headers");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [authorizeUrl, setAuthorizeUrl] = useState("");
  const [tokenUrl, setTokenUrl] = useState("");
  const [registrationUrl, setRegistrationUrl] = useState("");
  const [scopes, setScopes] = useState("");
  const [discovery, setDiscovery] = useState<MCPOAuthDiscovery | null>(null);
  const [relayGroupId, setRelayGroupId] = useState<string | null>(null);

  const isRemote = transport === "sse" || transport === "streamable_http";

  // Probe the server's OAuth metadata (RFC 9728 -> RFC 8414) and fill in what
  // it publishes. Only blanks are filled: anything already typed was typed on
  // purpose and overwriting it would be a surprise.
  const handleDiscover = () => {
    discover.mutate(
      {
        url: url.trim(),
        authorize_url: authorizeUrl.trim() || undefined,
        token_url: tokenUrl.trim() || undefined,
        relay_group_id: relayGroupId ?? undefined,
      },
      {
        onSuccess: (data) => {
          setDiscovery(data);
          if (data.authorize_url && !authorizeUrl.trim()) setAuthorizeUrl(data.authorize_url);
          if (data.token_url && !tokenUrl.trim()) setTokenUrl(data.token_url);
          if (data.registration_url && !registrationUrl.trim()) setRegistrationUrl(data.registration_url);
          if (data.scopes && !scopes.trim()) setScopes(data.scopes);
        },
        onError: (err) => {
          toast({ title: t_i18n("Detection failed"), description: err.message || t_i18n("Could not read the server's OAuth metadata."), variant: "error" });
        },
      },
    );
  };

  const selectPreset = (preset: MCPPreset) => {
    setSelectedPreset(preset);
    setName(preset.name);
    setTransport(preset.transport);
    setCommand(preset.command ?? "");
    setArgs(argsFieldText(preset.args));
    setArgsPlaceholder(null);
    setUrl(preset.url ?? "");
    // Pre-fill env var keys from the preset (user just needs to fill values)
    if (preset.envVars.length > 0) {
      setEnvPairs(preset.envVars.map((envVar: { key: string; placeholder: string; description: string }) => ({ key: envVar.key, value: "" })));
    } else {
      setEnvPairs([{ key: "", value: "" }]);
    }
    setHeaderPairs([{ key: "", value: "" }]);
    // The hosted presets (VirusTotal, Recorded Future, Atlassian, ...) all
    // authenticate with OAuth rather than a static header, so land the user on
    // that tab instead of making them switch before they can connect.
    setAuthMode(preset.transport === "stdio" ? "headers" : "oauth");
    setClientId("");
    setClientSecret("");
    setAuthorizeUrl("");
    setTokenUrl("");
    setRegistrationUrl("");
    setScopes("");
    setDiscovery(null);
    setShowSecrets(false);
    setStep("config");
  };

  const selectCustom = () => {
    setSelectedPreset(null);
    setName("");
    setTransport("stdio");
    setCommand("");
    setArgs("");
    setArgsPlaceholder(null);
    setUrl("");
    setEnvPairs([{ key: "", value: "" }]);
    setHeaderPairs([{ key: "", value: "" }]);
    setAuthMode("headers");
    setClientId("");
    setClientSecret("");
    setAuthorizeUrl("");
    setTokenUrl("");
    setRegistrationUrl("");
    setScopes("");
    setDiscovery(null);
    setShowSecrets(false);
    setStep("config");
  };

  const handleCreate = async () => {
    // Say what is missing instead of silently doing nothing. The name field is
    // at the top of a long form and the button at the bottom, so "the button
    // does nothing" is all the user can observe — send them to the field.
    if (!name.trim()) {
      setNameError(true);
      nameInputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      nameInputRef.current?.focus();
      return;
    }
    // A preset's documentation path, left as is, creates a server that looks
    // configured and fails at its first tool call - send the user to the field.
    const placeholder = transport === "stdio" ? findPlaceholderPath(args) : null;
    if (placeholder) {
      setArgsPlaceholder(placeholder);
      argsInputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      argsInputRef.current?.focus();
      return;
    }
    const env: Record<string, string> = {};
    for (const pair of envPairs) {
      if (pair.key.trim() && pair.value.trim()) env[pair.key.trim()] = pair.value.trim();
    }
    const headers: Record<string, string> = {};
    for (const pair of headerPairs) {
      if (pair.key.trim() && pair.value.trim()) headers[pair.key.trim()] = pair.value.trim();
    }

    const useOAuth = isRemote && authMode === "oauth";
    // Creating it company-managed lends its headers or environment to
    // everyone from the start.
    if (isCompanyManaged && !(await confirmSharing({
      entity_type: "mcp_server",
      change: {
        name: name.trim(),
        is_company_managed: true,
        auth_mode: isRemote ? authMode : undefined,
        holds_secret: Object.keys(env).length > 0 || (!useOAuth && Object.keys(headers).length > 0),
      },
    }))) return;
    create.mutate(
      {
        name: name.trim(),
        transport,
        command: transport === "stdio" && command.trim() ? command.trim() : undefined,
        args: transport === "stdio" && args.trim() ? splitArgs(args) : undefined,
        url: isRemote && url.trim() ? url.trim() : undefined,
        env: Object.keys(env).length > 0 ? env : undefined,
        headers: !useOAuth && Object.keys(headers).length > 0 ? headers : undefined,
        auth_mode: isRemote ? authMode : undefined,
        client_id: useOAuth && clientId.trim() ? clientId.trim() : undefined,
        client_secret: useOAuth && clientSecret.trim() ? clientSecret.trim() : undefined,
        authorize_url: useOAuth && authorizeUrl.trim() ? authorizeUrl.trim() : undefined,
        token_url: useOAuth && tokenUrl.trim() ? tokenUrl.trim() : undefined,
        registration_url: useOAuth && registrationUrl.trim() ? registrationUrl.trim() : undefined,
        scopes: useOAuth && scopes.trim() ? scopes.trim() : undefined,
        tags: newTags.length > 0 ? newTags : undefined,
        is_company_managed: isCompanyManaged || undefined,
        relay_group_id: isRemote && relayGroupId ? relayGroupId : undefined,
      },
      {
        onSuccess: (data) => {
          const srv = data as MCPServer;
          toast({ title: t_i18n("Server created"), description: t_i18n("{name} has been created.", { name: srv.name }), variant: "success" });
          navigate(`/mcp-servers/${srv.id}`);
        },
      },
    );
  };

  // ---- Step 1: Preset Picker ----
  if (step === "select") {
    return (
      <>
        {consentElement}
      <div className="space-y-6">
        <BackButton to="/mcp-servers" label="Back to MCP Servers" />

        <div>
          <h1 className="text-2xl font-bold">{t_i18n("Add MCP Server")}</h1>
          <p className="mt-2 text-default-secondary">{t_i18n("Choose a preset for quick setup, or configure a custom server.")}</p>
        </div>

        <PresetPicker onSelectPreset={selectPreset} onSelectCustom={selectCustom} />
      </div>
      </>
    );
  }

  // ---- Step 2: Configuration Form ----
  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" onClick={() => setStep("select")}>
        <ChevronLeft className="h-4 w-4" /> {t_i18n("Back to Presets")}
      </Button>

      <div className="flex items-center gap-4">
        {selectedPreset && (
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            {selectedPreset.logo ? (
              <img src={selectedPreset.logo} alt="" className="h-6 w-6 object-contain" />
            ) : null}
          </div>
        )}
        <div>
          <h1 className="text-2xl font-bold">{selectedPreset ? t_i18n("Configure {name}", { name: selectedPreset.name }) : t_i18n("Custom MCP Server")}</h1>
          <p className="mt-0.5 content-base text-default-secondary">
            {selectedPreset ? t_i18n(selectedPreset.description) : t_i18n("Configure a custom MCP tool server.")}
          </p>
        </div>
      </div>

      {/* A preset launched with `docker run` cannot start on the official images */}
      {selectedPreset && presetNeedsDockerCli(selectedPreset) && (
        <div className="rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 p-3 flex gap-2.5">
          <TriangleAlert className="h-4 w-4 shrink-0 text-feedback-warning-primary mt-0.5" />
          <p className="content-compact text-feedback-warning-primary leading-relaxed">
            {t_i18n("This server starts with docker run, and the official XTM One images do not include the Docker CLI: it only starts on a platform image you build with the Docker CLI, with access to a Docker daemon.")}
          </p>
        </div>
      )}

      {/* Preset note */}
      {selectedPreset?.notes && (
        <div className="rounded-sm bg-accent/5 p-3 flex gap-2.5">
          <Info className="h-4 w-4 shrink-0 text-accent mt-0.5" />
          <p className="content-compact text-accent/90 leading-relaxed">{t_i18n(selectedPreset.notes)}</p>
        </div>
      )}

      <Card>
        <CardContent className="pt-6 space-y-5">
          {/* Name and Tags side by side; Transport on its own row: its three buttons
              do not fit a third of the card, and Tags painted over them. */}
          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField
              id="new-server-name"
              label={t_i18n("Server Name")}
              required
              helper={t_i18n("Shown in lists and to agents that use its tools.")}
              error={nameError ? t_i18n("Enter a name for this server.") : undefined}
            >
              <Input id="new-server-name" aria-label={t_i18n("Server Name")}
                ref={nameInputRef}
                placeholder={t_i18n("e.g. GitHub MCP")}
                value={name}
                onChange={(e) => { setName(e.target.value); if (nameError) setNameError(false); }}
                aria-invalid={nameError || undefined}
                aria-describedby={settingsFieldHintId("new-server-name")}
              />
            </SettingsField>
            <SettingsField id="new-server-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-server-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-server-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
            <TransportPicker value={transport} onChange={setTransport} />
          </div>

          {/* Connection fields */}
          {transport === "stdio" ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <SettingsField
                id="new-server-command"
                label={t_i18n("Command")}
                helper={t_i18n("The program that starts the server, for example npx, uvx or docker.")}
                info={t_i18n("Runs as a subprocess of the platform, so it must be installed where the platform runs. Without a command the server cannot start.")}
              >
                <Input id="new-server-command" aria-labelledby={settingsFieldLabelId("new-server-command")} aria-describedby={settingsFieldHintId("new-server-command")} value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" className="font-mono-plex" />
              </SettingsField>
              <SettingsField
                id="new-server-args"
                label={t_i18n("Arguments (space-separated)")}
                helper={t_i18n("Passed to the command in this order, separated by spaces.")}
                info={t_i18n("For example -y @modelcontextprotocol/server-github. Replace any placeholder path with a real path before creating the server.")}
                error={argsPlaceholder ? t_i18n("Replace {path} in the arguments with a real path before creating the server.", { path: argsPlaceholder }) : undefined}
              >
                <Input id="new-server-args" aria-labelledby={settingsFieldLabelId("new-server-args")} aria-describedby={settingsFieldHintId("new-server-args")}
                  ref={argsInputRef}
                  value={args}
                  onChange={(e) => { setArgs(e.target.value); if (argsPlaceholder) setArgsPlaceholder(null); }}
                  placeholder="-y @modelcontextprotocol/server-github"
                  aria-invalid={argsPlaceholder ? true : undefined}
                  className="font-mono-plex"
                />
              </SettingsField>
            </div>
          ) : (
            <SettingsField
              id="new-server-url"
              required
              label={transport === "streamable_http" ? t_i18n("Server URL") : t_i18n("SSE URL")}
              helper={t_i18n("The address this platform connects to.")}
              info={t_i18n("The full URL of the MCP endpoint, for example https://mcp.example.com/mcp. Also used to detect OAuth settings.")}
            >
              <Input id="new-server-url" aria-labelledby={settingsFieldLabelId("new-server-url")} aria-describedby={settingsFieldHintId("new-server-url")} aria-required value={url} onChange={(e) => setUrl(e.target.value)} placeholder={transport === "streamable_http" ? "https://mcp.example.com/mcp" : "https://mcp.example.com/sse"} className="font-mono-plex" />
            </SettingsField>
          )}
          {isRemote && <RelayRouteField value={relayGroupId} onChange={setRelayGroupId} destination={url} />}

          {/* Authentication mode selector (remote transports only) */}
          {isRemote && <AuthModePicker value={authMode} onChange={setAuthMode} />}

          {/* Secrets — env vars for stdio, headers for remote+headers mode */}
          {(transport === "stdio" || (isRemote && authMode === "headers")) && (
            transport === "stdio" ? (
              <KeyValueEditor
                pairs={envPairs}
                onChange={setEnvPairs}
                showSecrets={showSecrets}
                onToggleSecrets={setShowSecrets}
                label="Environment Variables (secrets)"
                helper="Passed to the server's process when it starts; a value can come from a vault."
                info="The name and value of each variable the server reads, e.g. GITHUB_TOKEN. A row with an empty name or value is not saved. Values stay hidden until you choose Show."
                keyLabel="Variable Name"
                valueLabel="Value"
                keyPlaceholders={envPairs.map((_, i) => selectedPreset?.envVars[i]?.key || "ENV_VAR_NAME")}
                valuePlaceholders={envPairs.map((_, i) => selectedPreset?.envVars[i]?.placeholder || "secret value")}
                descriptions={envPairs.map((_, i) => selectedPreset?.envVars[i]?.description)}
                addLabel="+ Add another variable"
                allowVault
              />
            ) : (
              <KeyValueEditor
                pairs={headerPairs}
                onChange={setHeaderPairs}
                showSecrets={showSecrets}
                onToggleSecrets={setShowSecrets}
                label="HTTP Headers (secrets)"
                helper="Sent with every request to the server; a value can come from a vault."
                info="The name and value of each header, e.g. Authorization with Bearer and the token. A row with an empty name or value is not saved. Values stay hidden until you choose Show."
                keyLabel="Header Name"
                valueLabel="Header Value"
                keyPlaceholders={headerPairs.map((_, i) => i === 0 ? "Authorization" : "Header-Name")}
                valuePlaceholders={headerPairs.map((_, i) => i === 0 ? "Bearer ..." : "value")}
                addLabel="+ Add another header"
                allowVault
              />
            )
          )}

          {/* OAuth configuration (remote transports with oauth auth mode) */}
          {isRemote && authMode === "oauth" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="content-compact font-medium text-default-secondary">{t_i18n("OAuth 2.0 Configuration")}</span>
                <div className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 content-compact"
                    onClick={handleDiscover}
                    disabled={!url.trim() || discover.isPending}
                  >
                    <Search className="h-3 w-3 mr-1" />
                    {discover.isPending ? t_i18n("Detecting…") : t_i18n("Detect settings")}
                  </Button>
                  <Button variant="ghost" size="sm" className="h-6 px-2 content-compact" onClick={() => setShowSecrets(!showSecrets)}>
                    {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
                    {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
                  </Button>
                </div>
              </div>

              <div className="rounded-sm bg-accent/5 p-3 flex gap-2.5">
                <Info className="h-4 w-4 shrink-0 text-accent mt-0.5" />
                <p className="content-compact text-accent/90 leading-relaxed">
                  {t_i18n("Client credentials are optional. Leave them empty and this platform registers itself with the server automatically when you authorize the connection: you only need the Authorize and Token URLs, and even those are detected from the server URL when it publishes OAuth metadata.")}
                </p>
              </div>

              {discovery && (
                <div className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary">
                  {discovery.supports_dynamic_registration
                    ? discovery.registration_url_advertised
                      ? t_i18n("This server supports automatic client registration: no client ID or secret needed.")
                      : t_i18n("This server publishes no registration endpoint, so the standard one will be tried. If it is refused, enter a client ID below.")
                    : discovery.discovered
                      ? t_i18n("OAuth endpoints detected. This server does not offer automatic registration, so enter a client ID below.")
                      : t_i18n("No OAuth metadata published by this server. Enter the Authorize and Token URLs manually.")}
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <SettingsField
                  id="new-server-authorize-url"
                  label={t_i18n("Authorize URL")}
                  helper={t_i18n("Where you sign in to grant this platform access.")}
                  info={t_i18n("The OAuth authorization endpoint, for example https://provider.com/oauth/authorize. Empty: detected from the server URL when the server publishes OAuth metadata.")}
                >
                  <Input id="new-server-authorize-url" aria-labelledby={settingsFieldLabelId("new-server-authorize-url")} aria-describedby={settingsFieldHintId("new-server-authorize-url")}
                    value={authorizeUrl}
                    onChange={(e) => setAuthorizeUrl(e.target.value)}
                    placeholder="https://provider.com/oauth/authorize"
                    className="font-mono-plex"
                  />
                </SettingsField>
                <SettingsField
                  id="new-server-token-url"
                  label={t_i18n("Token URL")}
                  helper={t_i18n("Where this platform exchanges the grant for an access token.")}
                  info={t_i18n("The OAuth token endpoint, for example https://provider.com/oauth/token. Empty: detected from the server URL when the server publishes OAuth metadata.")}
                >
                  <Input id="new-server-token-url" aria-labelledby={settingsFieldLabelId("new-server-token-url")} aria-describedby={settingsFieldHintId("new-server-token-url")}
                    value={tokenUrl}
                    onChange={(e) => setTokenUrl(e.target.value)}
                    placeholder="https://provider.com/oauth/token"
                    className="font-mono-plex"
                  />
                </SettingsField>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <SettingsField
                  id="new-server-registration-url"
                  label={t_i18n("Registration URL (optional)")}
                  helper={t_i18n("Where this platform registers itself as a client.")}
                  info={t_i18n("The dynamic client registration endpoint, for example https://provider.com/oauth/register. Empty: the one the server publishes, else the standard path.")}
                >
                  <Input id="new-server-registration-url" aria-labelledby={settingsFieldLabelId("new-server-registration-url")} aria-describedby={settingsFieldHintId("new-server-registration-url")}
                    value={registrationUrl}
                    onChange={(e) => setRegistrationUrl(e.target.value)}
                    placeholder="https://provider.com/oauth/register"
                    className="font-mono-plex"
                  />
                </SettingsField>
                <SettingsField
                  id="new-server-scopes"
                  label={t_i18n("Scopes (space-separated)")}
                  helper={t_i18n("The permissions asked for when you authorize the connection.")}
                  info={t_i18n("Separate scopes with a space, for example read write openid. Empty: the server's default scopes.")}
                >
                  <Input id="new-server-scopes" aria-labelledby={settingsFieldLabelId("new-server-scopes")} aria-describedby={settingsFieldHintId("new-server-scopes")}
                    value={scopes}
                    onChange={(e) => setScopes(e.target.value)}
                    placeholder="read write openid"
                    className="font-mono-plex"
                  />
                </SettingsField>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <SettingsField
                  id="new-server-client-id"
                  label={t_i18n("Client ID (optional)")}
                  helper={t_i18n("The client this platform signs in as.")}
                  info={t_i18n("The client ID the provider gave you when you registered this platform. Empty: this platform registers itself automatically when you authorize the connection.")}
                >
                  <Input id="new-server-client-id" aria-labelledby={settingsFieldLabelId("new-server-client-id")} aria-describedby={settingsFieldHintId("new-server-client-id")}
                    type={showSecrets ? "text" : "password"}
                    value={clientId}
                    onChange={(e) => setClientId(e.target.value)}
                    placeholder={t_i18n("Registered automatically")}
                    className="font-mono-plex"
                  />
                </SettingsField>
                <SettingsField
                  id="new-server-client-secret"
                  label={t_i18n("Client Secret (optional)")}
                  helper={t_i18n("Only for a confidential client.")}
                  info={t_i18n("The secret paired with the client ID, for a confidential client. Empty: a public client, or the one registered automatically.")}
                >
                  <SecretField
                    id="new-server-client-secret"
                    aria-label={t_i18n("Client Secret (optional)")}
                    aria-describedby={settingsFieldHintId("new-server-client-secret")}
                    value={clientSecret}
                    onChange={setClientSecret}
                    placeholder={t_i18n("Not needed for public clients")}
                  />
                </SettingsField>
              </div>
            </div>
          )}

          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed server")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed server")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => setStep("select")}>{t_i18n("Cancel")}</Button>
            {/* Only disabled while the request is in flight. A button greyed
                out for a reason it cannot state is unanswerable: the user can
                neither click it nor learn what is missing. */}
            <AiDisabledTip>
            <Button onClick={handleCreate} disabled={create.isPending || aiOff}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create Server")}
            </Button>
            </AiDisabledTip>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
