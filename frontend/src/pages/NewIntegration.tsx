// SPDX-License-Identifier: Apache-2.0

import { platformOrigin } from "@/lib/base-path";
import { randomId } from "@/lib/random-id";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import {
  ChevronLeft, Shield, Eye, EyeOff, CheckCircle2, Building2, Webhook, Copy, Check,
  ArrowDownLeft, ArrowUpRight,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { RelayRouteField } from "@/components/ui/relay-route-field";
import { serviceUrlOf } from "@/lib/relay-destinations";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Input } from "@/components/ui/input";
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { TagInput } from "@/components/ui/tag-input";
import { PageSkeleton } from "@/components/ui/skeleton";
import { SetupGuideDialog } from "@/components/ui/setup-guide-dialog";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useProviderCatalog, useCreateIntegration } from "@/hooks/api/useIntegrationApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { extraFieldDefaults, hasInvalidExtraField, withoutHiddenExtraFields } from "@/lib/extra-fields";
import { credentialFieldsFor, resolveVariant, setupGuideFor, variantValue } from "@/lib/catalog-variants";
import { offersSlackEventsUrl } from "@/lib/slack-events";
import { ExtraFieldInputs } from "@/components/ui/extra-field-inputs";
import { OpenAPIIntegrationForm, type OpenAPIDraft } from "@/components/integrations/openapi/openapi-integration-form";
import { Label } from "@/components/ui/label";
import { SecretField } from "@/components/ui/secret-field";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { createsOnAdminOAuthApp } from "@/pages/integration-detail/admin-oauth-app";
import { hasWebhookDeliveredEvents } from "@/pages/integration-detail/webhooks-card-blocks";
import type { ProviderCatalogItem, CreateIntegrationRequest } from "@/types";

/**
 * The configuration step of creating an integration.
 *
 * Browsing lives on /integrations, which is the catalog: this page is reached
 * with the provider already chosen (`?provider=<key>`), and a visit without
 * one belongs back on the catalog rather than on a second copy of it.
 */
/** The first paragraph of an API description, whitespace collapsed and capped. */
function firstParagraph(text: string, limit = 300): string {
  const paragraph = (text.split(/\n\s*\n/)[0] ?? "").replace(/\s+/g, " ").trim();
  if (paragraph.length <= limit) return paragraph;
  const cut = paragraph.slice(0, limit);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), limit / 2)).trimEnd()}…`;
}

export default function NewIntegration() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const providerKey = searchParams.get("provider");
  const { t_i18n } = useFormatter();
  const { data: catalog, isLoading } = useProviderCatalog("integration", true);
  const { data: authConfig } = useAuthConfig();
  const { isAdmin } = useAuth();
  const create = useCreateIntegration();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const { toast } = useToast();

  const preGeneratedId = useMemo(() => randomId(), []);
  const baseUrl = authConfig?.base_url ?? platformOrigin();

  const [selected, setSelected] = useState<ProviderCatalogItem | null>(null);
  const [showSecrets, setShowSecrets] = useState(false);
  const [form, setForm] = useState<CreateIntegrationRequest>({ provider: "", name: "" });
  const [extraFields, setExtraFields] = useState<Record<string, unknown>>({});
  const [useOwnCreds, setUseOwnCreds] = useState(false);
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);
  const [copiedWebhookUrl, setCopiedWebhookUrl] = useState(false);
  const [selectedAuthType, setSelectedAuthType] = useState<string | null>(null);
  // OpenAPI integrations: the spec form reports what it contributes to the payload.
  const [openapiDraft, setOpenapiDraft] = useState<OpenAPIDraft | null>(null);
  const suggestName = useCallback((title: string) => {
    setForm((current) => (current.name.trim() ? current : { ...current, name: title }));
  }, []);
  const suggestDescription = useCallback((description: string) => {
    setForm((current) =>
      current.description?.trim() ? current : { ...current, description: firstParagraph(description) },
    );
  }, []);

  // The credential fields to ask for, which for an A2A mechanism depend on the
  // direction: inbound OAuth asks for nothing because the platform mints the
  // secret, while outbound needs a card location and the partner's own
  // credentials. Computed once rather than at each render site, because a
  // site that kept reading `selected.extra_fields` would silently omit them.
  //
  // The entry as it reads for the choice made on the form (the direction,
  // for A2A). A variant's fields replace the base map rather than merging.
  const resolved = useMemo(
    () => (selected ? resolveVariant(selected, variantValue(selected, extraFields, form.a2a_direction)) : undefined),
    [selected, extraFields, form.a2a_direction],
  );
  const effectiveExtraFields = resolved?.extra_fields ?? null;
  // One help line under every credential field (#11837): where the value comes from.
  const credentialHelp = selected
    ? resolved?.setup_guide
      ? t_i18n("From your {provider} account: the Setup guide above shows where.", { provider: selected.name })
      : t_i18n("From your {provider} account settings.", { provider: selected.name })
    : undefined;
  const extraFieldHelp = () => credentialHelp;

  const selectProvider = useCallback((item: ProviderCatalogItem) => {
    if (item.is_disabled) return;
    setSelected(item);
    setForm({
      provider: item.provider,
      name: "",
      auth_type: item.auth_type,
      // Defaulted rather than left unset. The backend also defaults to
      // inbound, so an unset value would silently make an inbound
      // integration — and for a mechanism that only serves one direction
      // there is nothing to ask.
      ...(item.a2a_directions?.length
        ? { a2a_direction: item.a2a_directions[0] as "inbound" | "outbound" }
        : {}),
    });
    // Pre-populate extra fields with default values from extra_field_options
    const defaults: Record<string, string> = extraFieldDefaults(item.extra_field_inputs);
    if (item.extra_field_options) {
      for (const [key, options] of Object.entries(item.extra_field_options)) {
        if (options.length > 0) defaults[key] = options[0].value;
      }
    }
    setExtraFields(defaults);
    setShowSecrets(false);
    setUseOwnCreds(false);
    setIsCompanyManaged(Boolean(item.company_managed_only));
    setSelectedAuthType(item.supported_auth_types ? item.auth_type : null);
    setOpenapiDraft(null);
  }, []);

  // Registration-only providers are never creatable, and an admin-disabled
  // one is not either — the catalog shows both, this page refuses both.
  const requested = catalog?.find((c) => c.provider === providerKey);
  const creatable = Boolean(requested && !requested.is_disabled && !requested.is_registration_only);

  useEffect(() => {
    if (requested && creatable) selectProvider(requested);
  }, [requested, creatable, selectProvider]);


  if (isLoading) return <PageSkeleton cards={6} />;
  if (!creatable) return <Navigate to="/integrations" replace />;


  const isOpenAPI = selected?.spec_import === "openapi";
  // OAuth tokens are personal (and platform-managed ones per user), so
  // company-managed stays off for them — for an OpenAPI integration the spec's
  // authentication option decides which one applies.
  const effectiveAuthMode = isOpenAPI ? (openapiDraft?.authType ?? "api_key") : (selectedAuthType || selected?.auth_type);
  // The guide of the mode chosen in the form, when the provider sets its modes up apart.
  const setupGuide = setupGuideFor(resolved, effectiveAuthMode);
  // The credential fields of the mode chosen in the form, not of the default one (#2317).
  const credentialFields = selected ? credentialFieldsFor(selected, effectiveAuthMode) : [];

  const handleCreate = async () => {
    if (!selected) return;
    if (!form.name.trim()) return;
    if (isOpenAPI) {
      if (!openapiDraft?.ready) return;
      const openapiPayload: CreateIntegrationRequest = {
        id: preGeneratedId,
        provider: selected.provider,
        name: form.name.trim(),
        description: form.description?.trim() || undefined,
        tags: newTags.length > 0 ? newTags : undefined,
        is_company_managed: (isCompanyManaged && openapiDraft.authType !== "oauth") || undefined,
        ...openapiDraft.payload,
      } as CreateIntegrationRequest;
      // A company-managed integration lends what it stores to every user.
      if (openapiPayload.is_company_managed && !(await confirmSharing(
        {
          entity_type: "integration",
          change: {
            name: openapiPayload.name,
            provider: openapiPayload.provider,
            auth_type: openapiDraft.authType,
            is_company_managed: true,
            holds_secret: !!(
              openapiPayload.api_key || openapiPayload.client_secret || openapiPayload.extra_config
            ),
          },
        },
        { confirmLabel: t_i18n("Create") },
      ))) {
        return;
      }
      create.mutate(openapiPayload, {
        onSuccess: (created) => {
          toast({ title: t_i18n("Integration created"), variant: "success" });
          navigate(`/integrations/${created.id}`);
        },
      });
      return;
    }
    const effectiveAuth = selectedAuthType || form.auth_type;
    let effectiveAuthType = effectiveAuth;
    if (effectiveAuth !== "platform_managed" && effectiveAuth !== "none" && form.api_key && !form.client_id) effectiveAuthType = "api_key";

    // NOT named ``effectiveExtraFields`` (the field MAP for the current
    // direction): only values the current form asked for may reach the
    // payload — state accumulated before a direction change must not.
    const enteredValues: Record<string, unknown> = { ...extraFields };
    if (selected.settings_schema?.length) {
      for (const field of selected.settings_schema) {
        if (field.type === "select" && field.options?.length && enteredValues[field.key] == null) {
          enteredValues[field.key] = field.options[0].value;
        }
      }
    }

    let inferredApiKey: string | undefined;
    if (!form.api_key && selected.settings_schema?.length) {
      const secretCandidate = selected.settings_schema.find(
        (s) => s.type === "secret" && ["api_key", "token", "bot_token"].includes(s.key.toLowerCase()),
      );
      const candidateValue = secretCandidate ? enteredValues[secretCandidate.key] : undefined;
      if (secretCandidate && typeof candidateValue === "string" && candidateValue.trim()) {
        inferredApiKey = candidateValue.trim();
        // The credential now lives in api_key (encrypted, dedicated column);
        // keeping it in extra_config would persist and expose it twice.
        delete enteredValues[secretCandidate.key];
      }
    }

    // Only the fields THIS form asked for. A settings-schema provider
    // declares its keys there; everything else declares them in the field map
    // for the direction in play. Anything else in state belongs to a form the
    // operator has since left.
    const askedFor = new Set([
      ...Object.keys(effectiveExtraFields ?? {}),
      ...(selected.settings_schema ?? []).map((f) => f.key),
    ]);
    const submittedExtraFields = withoutHiddenExtraFields(
      Object.fromEntries(
        Object.entries(enteredValues).filter(([key]) => askedFor.has(key)),
      ),
      selected.extra_field_visible_when,
      selected.extra_field_options,
    );

    const payload: CreateIntegrationRequest = {
      ...form,
      id: preGeneratedId,
      api_key: form.api_key || inferredApiKey,
      auth_type: effectiveAuthType,
      // Hidden fields are omitted, not sent empty: they belong to an answer
      // the operator did not pick, and storing one would leave a credential
      // that nothing uses.
      extra_config: Object.keys(submittedExtraFields).length > 0 ? submittedExtraFields : undefined,
      tags: newTags.length > 0 ? newTags : undefined,
      // Company-managed is incompatible with user-specific auth (OAuth) and
      // platform_managed; the toggle is hidden in those modes, so clamp here to
      // guarantee a stale flag is never submitted regardless of mode switches.
      is_company_managed:
        (isCompanyManaged &&
          effectiveAuthType !== "oauth" &&
          effectiveAuthType !== "platform_managed") ||
        undefined,
    } as CreateIntegrationRequest;
    // A company-managed integration lends what it stores to every user.
    if (payload.is_company_managed && !(await confirmSharing(
      {
        entity_type: "integration",
        change: {
          name: payload.name,
          provider: payload.provider,
          auth_type: effectiveAuthType,
          is_company_managed: true,
          holds_secret: !!(payload.api_key || payload.extra_config),
        },
      },
      { confirmLabel: t_i18n("Create") },
    ))) {
      return;
    }
    create.mutate(payload, { onSuccess: () => { toast({ title: t_i18n("Integration created"), variant: "success" }); navigate("/integrations?vis=mine"); } });
  };

  if (selected) {
    // Microsoft and Slack events never reach assignments through the
    // integration hook: Microsoft ones are polled, Slack ones are polled or
    // arrive on the notifications endpoint whichever Slack app sends them.
    const isMicrosoft = selected.provider === "microsoft";
    const isSlack = selected.provider === "slack";
    const slackEventsUrl = `${baseUrl}/api/v1/integrations/notifications/slack`;
    const onAdminOAuthApp = createsOnAdminOAuthApp(selected, selectedAuthType || form.auth_type, form.client_id);
    // The webhook URL and the admin-managed webhook line only hold for events
    // that can arrive by webhook; a provider whose events are all polled gets
    // one polling line instead.
    const hasOwnEventsLine = isMicrosoft || isSlack;
    const webhookEvents = !hasOwnEventsLine && hasWebhookDeliveredEvents(selected);
    const pollingOnlyEvents = !hasOwnEventsLine && selected.available_events.length > 0 && !webhookEvents;
    return (
      <div className="space-y-6">
        {consentElement}
        <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" onClick={() => navigate("/integrations")}>
          <ChevronLeft className="h-4 w-4" /> {t_i18n("Back to Integrations")}
        </Button>
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            {selected.logo && <img src={selected.logo} alt="" className="h-8 w-8" />}
            <div>
              <h1 className="text-2xl font-bold">{selected.name}</h1>
              <p className="mt-2 text-default-secondary">{selected.description}</p>
            </div>
          </div>
          {setupGuide && (
            <SetupGuideDialog guide={setupGuide} providerName={selected.name} variant="outline" />
          )}
        </div>

        <Card>
          <CardContent className="pt-6 space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label className="block text-default-primary">{t_i18n("Name")}</Label>
                <Input aria-label={t_i18n("Name")} placeholder={t_i18n("e.g. My {provider}", { provider: selected.name })} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>
              <div className="space-y-1.5">
                <Label className="block text-default-primary">{t_i18n("Tags")}</Label>
                <TagInput value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
              </div>
            </div>

            <RelayRouteField
              value={form.relay_group_id ?? null}
              onChange={(relay_group_id) => setForm((f) => ({ ...f, relay_group_id }))}
              destination={serviceUrlOf(extraFields)}
              required={!!selected.requires_relay}
            />

            {/* A2A only, and only when the mechanism serves both. Direction is
                the most consequential thing about a partner relationship — it
                decides whether binding this to an agent exposes that agent or
                gives it a tool — so it sits with Name rather than among the
                credential fields, and it cannot be changed later while the
                integration is bound. */}
            {(selected.a2a_directions?.length ?? 0) > 1 && (
              <div className="space-y-1.5">
                <Label className="block text-default-primary">
                  {t_i18n("Direction")}
                </Label>
                <div className="flex gap-2">
                  {selected.a2a_directions!.map((direction) => {
                    const active = form.a2a_direction === direction;
                    return (
                      <button
                        key={direction}
                        type="button"
                        // These are toggle buttons, and until now the selection
                        // lived only in the paint: nothing announced which
                        // direction was chosen, and the end-to-end suite had to
                        // read a background class to find out. `aria-pressed`
                        // states it once, for assistive technology and for the
                        // test alike.
                        aria-pressed={active}
                        onClick={() =>
                          setForm({
                            ...form,
                            a2a_direction: direction as "inbound" | "outbound",
                          })
                        }
                        className={cn(
                          "flex-1 rounded-sm border p-3 text-left transition-colors",
                          active
                            ? "border-accent bg-accent/10"
                            : "border-elevation-subtle-soft hover:bg-elevation-hover",
                        )}
                      >
                        <span className="flex items-center gap-1.5 content-compact font-medium">
                          {direction === "inbound" ? (
                            <ArrowDownLeft className="h-3.5 w-3.5" />
                          ) : (
                            <ArrowUpRight className="h-3.5 w-3.5" />
                          )}
                          {direction === "inbound"
                            ? t_i18n("Inbound: a partner tasks our agents")
                            : t_i18n("Outbound: our agents task a partner")}
                        </span>
                        <span className="mt-1 block content-compact text-default-secondary">
                          {direction === "inbound"
                            ? t_i18n("Binding this to an agent exposes that agent to the partner.")
                            : t_i18n("Binding this to an agent gives it a tool to delegate work to the partner.")}
                        </span>
                      </button>
                    );
                  })}
                </div>
                <p className="content-compact text-default-secondary">
                  {t_i18n("An integration serves one direction. Changing it later requires unbinding it from every agent first.")}
                </p>
              </div>
            )}

            {isOpenAPI && (
              <div className="space-y-1.5">
                <Label className="block text-default-primary">{t_i18n("Description")}</Label>
                <textarea
                  rows={2}
                  value={form.description ?? ""}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  placeholder={t_i18n("What this API gives access to. Agents read it as context for its tools.")}
                  className="w-full resize-none rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base ring-offset-focus placeholder:text-default-disabled focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2"
                />
              </div>
            )}

            {isOpenAPI ? (
              <OpenAPIIntegrationForm
                onDraftChange={setOpenapiDraft}
                onTitle={suggestName}
                onDescription={suggestDescription}
                callbackUrl={`${baseUrl}/api/oauth/callback`}
              />
            ) : (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Shield className="h-4 w-4 text-default-secondary" />
                <span className="content-base font-medium">{t_i18n("Credentials")}</span>
                {!(selected.has_oauth_defaults && !useOwnCreds) && (selectedAuthType || "api_key") !== "platform_managed" && (
                  <Button variant="ghost" size="sm" className="ml-auto h-6 px-2 content-compact" onClick={() => setShowSecrets(!showSecrets)}>
                    {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}{showSecrets ? t_i18n("Hide") : t_i18n("Show")}
                  </Button>
                )}
              </div>

              {/* Auth mode selector — shown when provider supports multiple auth types */}
              {selected.supported_auth_types && selected.supported_auth_types.length > 1 && (
                <div className="space-y-1.5">
                  <Label className="block text-default-primary">{t_i18n("Authentication mode")}</Label>
                  <Select value={selectedAuthType || selected.auth_type} onValueChange={(v) => { setSelectedAuthType(v); const kept = credentialFieldsFor(selected, v); setForm({ ...form, auth_type: v, ...(kept.includes("api_key") ? {} : { api_key: "" }), ...(kept.includes("client_id") ? {} : { client_id: "", client_secret: "" }) }); if (v === "oauth" || v === "platform_managed") setIsCompanyManaged(false); }}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {selected.supported_auth_types.map((at) => (
                        <SelectItem key={at} value={at}>
                          {at === "api_key" ? t_i18n("API key") : at === "platform_managed" ? t_i18n("Managed by the platform") : at === "oauth" ? t_i18n("OAuth") : at === "none" ? t_i18n("None (unauthenticated)") : at}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}

              {selected.is_custom && (selected.settings_schema?.length ?? 0) > 0 && (
                <div className="space-y-3 rounded-sm border border-elevation-subtle-soft p-3">
                  <p className="content-compact font-medium text-default-secondary">{t_i18n("Bundle settings")}</p>
                  {selected.settings_schema?.map((schemaField) => {
                    const value = extraFields[schemaField.key];
                    if (schemaField.type === "boolean") {
                      return (
                        <div key={schemaField.key} className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2">
                          <div>
                            <p className="content-compact font-medium">{schemaField.label}</p>
                            {schemaField.placeholder && (
                              <p className="content-compact text-default-secondary">{schemaField.placeholder}</p>
                            )}
                          </div>
                          <Switch aria-label={schemaField.label}
                            checked={Boolean(value)}
                            onCheckedChange={(checked) =>
                              setExtraFields({ ...extraFields, [schemaField.key]: checked })
                            }
                          />
                        </div>
                      );
                    }

                    if (schemaField.type === "select" && schemaField.options?.length) {
                      return (
                        <div key={schemaField.key} className="space-y-1">
                          <Label className="block text-default-primary">{schemaField.label}</Label>
                          <SearchableSelect
                            aria-label={schemaField.label}
                            searchable="auto"
                            sort={false}
                            options={schemaField.options}
                            value={String(value ?? schemaField.options[0]?.value ?? "")}
                            onValueChange={(v) => setExtraFields({ ...extraFields, [schemaField.key]: v })}
                          />
                        </div>
                      );
                    }

                    return (
                      <div key={schemaField.key} className="space-y-1">
                        <Label className="block text-default-primary">{schemaField.label}</Label>
                        <Input aria-label={schemaField.label}
                          type={schemaField.type === "secret" && !showSecrets ? "password" : "text"}
                          placeholder={schemaField.placeholder || schemaField.key}
                          value={typeof value === "string" ? value : ""}
                          onChange={(e) =>
                            setExtraFields({ ...extraFields, [schemaField.key]: e.target.value })
                          }
                        />
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Custom providers still need dedicated credential inputs: the
                  bundle settings above only cover extra_config, so OAuth
                  client credentials (and an api_key when the schema declares
                  no inferrable secret) must be collected explicitly. */}
              {selected.is_custom && (() => {
                const customAuth = selectedAuthType || form.auth_type || selected.auth_type;
                const hasInferrableSecret = (selected.settings_schema ?? []).some(
                  (s) => s.type === "secret" && ["api_key", "token", "bot_token"].includes(s.key.toLowerCase()),
                );
                if (customAuth === "oauth") {
                  return (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="space-y-1"><Label className="block text-default-primary">{t_i18n("Client ID")}</Label><Input aria-label={t_i18n("Client ID")} type={showSecrets ? "text" : "password"} placeholder={t_i18n("Paste client ID...")} value={form.client_id || ""} onChange={(e) => setForm({ ...form, client_id: e.target.value })} helperText={credentialHelp} /></div>
                      <SecretField label={t_i18n("Client Secret")} placeholder={t_i18n("Paste client secret...")} value={form.client_secret || ""} onChange={(client_secret) => setForm({ ...form, client_secret })} helperText={credentialHelp} />
                    </div>
                  );
                }
                if ((customAuth === "api_key" || customAuth === "bot_token") && !hasInferrableSecret) {
                  return (
                    <SecretField
                      label={customAuth === "bot_token" ? t_i18n("Bot token") : t_i18n("API key")}
                      placeholder={customAuth === "bot_token" ? t_i18n("Paste bot token...") : t_i18n("Paste API key...")}
                      value={form.api_key || ""}
                      onChange={(api_key) => setForm({ ...form, api_key })}
                      helperText={credentialHelp}
                    />
                  );
                }
                if (customAuth === "none" && (selected.settings_schema?.length ?? 0) === 0) {
                  return (
                    <div className="rounded-sm border border-alert-info bg-alert-info p-3">
                      <p className="content-compact text-default-primary">{t_i18n("No credentials needed: this integration will use the provider's free unauthenticated API (may be rate limited).")}</p>
                    </div>
                  );
                }
                return null;
              })()}

              {!selected.is_custom && (
                <>
              {/* JWT forwarding mode — no credentials, just header config */}
              {(selectedAuthType || form.auth_type) === "platform_managed" ? (
                <div className="space-y-3">
                  <div className="rounded-sm border border-alert-info bg-alert-info p-3">
                    <p className="content-compact text-default-primary">{t_i18n("Managed by the platform: No credentials needed. Authentication is handled automatically at runtime.")}</p>
                  </div>
                  {/* Extra fields (e.g. base_url) still shown */}
                  {effectiveExtraFields && (
                    <ExtraFieldInputs
                      fields={effectiveExtraFields}
                      values={extraFields}
                      onChange={setExtraFields}
                      options={selected.extra_field_options}
                      visibleWhen={selected.extra_field_visible_when}
                      inputs={selected.extra_field_inputs}
                      showSecrets={showSecrets}
                    />
                  )}
                </div>
              ) : /* Admin pre-configured OAuth — simplified flow */
              selected.has_oauth_defaults && effectiveAuthMode === "oauth" ? (
                <div className="space-y-3">
                  <div className="rounded-sm bg-feedback-success-primary/5 p-4 flex items-start gap-3">
                    <CheckCircle2 className="h-5 w-5 text-feedback-success-primary shrink-0 mt-0.5" />
                    <div>
                      <p className="content-base font-medium text-feedback-success-primary">
                        {selected.has_oauth_extra_config
                          ? t_i18n("Fully pre-configured by admin")
                          : t_i18n("OAuth credentials pre-configured by admin")}
                      </p>
                      <p className="content-compact text-default-secondary mt-1">
                        {selected.has_oauth_extra_config
                          ? t_i18n("Your admin has fully configured {provider} (credentials + settings). Just create the integration and authorize with your account.", { provider: selected.name })
                          : t_i18n("Your admin has already set up the Client ID & Secret for {provider}. Just create the integration and authorize with your account.", { provider: selected.name })}
                      </p>
                    </div>
                  </div>
                  {!useOwnCreds ? (
                    <button className="content-compact text-default-secondary hover:text-default-primary underline" onClick={() => setUseOwnCreds(true)}>
                      {t_i18n("I want to use my own Client ID & Secret instead")}
                    </button>
                  ) : (
                    <>
                      <button className="content-compact text-accent hover:text-accent/80 underline" onClick={() => { setUseOwnCreds(false); setForm({ ...form, client_id: "", client_secret: "" }); }}>
                        {t_i18n("Use admin pre-configured credentials")}
                      </button>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <div className="space-y-1"><Label className="block text-default-primary">{t_i18n("Client ID")}</Label><Input aria-label={t_i18n("Client ID")} type={showSecrets ? "text" : "password"} placeholder={t_i18n("Paste client ID...")} value={form.client_id || ""} onChange={(e) => setForm({ ...form, client_id: e.target.value })} helperText={credentialHelp} /></div>
                        <SecretField label={t_i18n("Client Secret")} placeholder={t_i18n("Paste client secret...")} value={form.client_secret || ""} onChange={(client_secret) => setForm({ ...form, client_secret })} helperText={credentialHelp} />
                      </div>
                    </>
                  )}

                  {/* Extra fields: hide webhook_secret when admin pre-provisioned (webhook managed centrally) */}
                  {effectiveExtraFields && (!selected.has_oauth_extra_config || useOwnCreds) && (
                    <ExtraFieldInputs
                      fields={effectiveExtraFields}
                      omitKeys={onAdminOAuthApp ? ["webhook_secret"] : undefined}
                      values={extraFields}
                      onChange={setExtraFields}
                      options={selected.extra_field_options}
                      visibleWhen={selected.extra_field_visible_when}
                      inputs={selected.extra_field_inputs}
                      showSecrets={showSecrets}
                    />
                  )}
                </div>
              ) : (selectedAuthType || form.auth_type) === "none" ? (
                /* Unauthenticated mode — no credentials to collect. Providers
                   whose whole point is their config (e.g. Entra A2A federation
                   pins, ATT&CK tuning knobs) still declare extra fields, which
                   were previously only editable after creation. */
                <div className="space-y-3">
                  {!effectiveExtraFields && (
                    <div className="rounded-sm border border-alert-info bg-alert-info p-3">
                      <p className="content-compact text-default-primary">{t_i18n("No credentials needed: this integration will use the provider's free unauthenticated API (may be rate limited).")}</p>
                    </div>
                  )}
                  {effectiveExtraFields && (
                    <ExtraFieldInputs
                      fields={effectiveExtraFields}
                      values={extraFields}
                      onChange={setExtraFields}
                      options={selected.extra_field_options}
                      visibleWhen={selected.extra_field_visible_when}
                      inputs={selected.extra_field_inputs}
                      showSecrets={showSecrets}
                    />
                  )}
                </div>
              ) : (
                /* Standard credential flow (no admin defaults) */
                <>
                  {credentialFields.includes("client_id") && credentialFields.includes("api_key") ? (
                    <div className="space-y-3">
                      <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
                        <p className="content-compact font-medium">{t_i18n("Option 1: Internal / API key")}</p>
                        <SecretField aria-label={t_i18n("API key")} placeholder={t_i18n("Paste API key or secret...")} value={form.api_key || ""} onChange={(api_key) => setForm({ ...form, api_key })} helperText={credentialHelp} />
                      </div>
                      <div className="flex items-center gap-3"><div className="flex-1 border-t border-elevation-subtle-soft" /><span className="content-compact text-default-secondary">{t_i18n("OR")}</span><div className="flex-1 border-t border-elevation-subtle-soft" /></div>
                      <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
                        <p className="content-compact font-medium">{t_i18n("Option 2: OAuth")}</p>
                        <div className="grid gap-3 sm:grid-cols-2">
                          <Input aria-label={t_i18n("Client ID")} type={showSecrets ? "text" : "password"} placeholder={t_i18n("Client ID")} value={form.client_id || ""} onChange={(e) => setForm({ ...form, client_id: e.target.value })} helperText={credentialHelp} />
                          <SecretField aria-label={t_i18n("Client Secret")} placeholder={t_i18n("Client Secret")} value={form.client_secret || ""} onChange={(client_secret) => setForm({ ...form, client_secret })} helperText={credentialHelp} />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <>
                      {credentialFields.includes("client_id") && (
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div className="space-y-1"><Label className="block text-default-primary">{t_i18n("Client ID")}</Label><Input aria-label={t_i18n("Client ID")} type={showSecrets ? "text" : "password"} placeholder={t_i18n("Paste client ID...")} value={form.client_id || ""} onChange={(e) => setForm({ ...form, client_id: e.target.value })} helperText={credentialHelp} /></div>
                          <SecretField label={t_i18n("Client Secret")} placeholder={t_i18n("Paste client secret...")} value={form.client_secret || ""} onChange={(client_secret) => setForm({ ...form, client_secret })} helperText={credentialHelp} />
                        </div>
                      )}
                      {credentialFields.includes("api_key") && (
                        <SecretField label={t_i18n("API key")} placeholder={t_i18n("Paste API key...")} value={form.api_key || ""} onChange={(api_key) => setForm({ ...form, api_key })} helperText={credentialHelp} />
                      )}
                    </>
                  )}

                  {effectiveExtraFields && (
                    <ExtraFieldInputs
                      fields={effectiveExtraFields}
                      values={extraFields}
                      onChange={setExtraFields}
                      options={selected.extra_field_options}
                      visibleWhen={selected.extra_field_visible_when}
                      inputs={selected.extra_field_inputs}
                      showSecrets={showSecrets}
                      helpFor={extraFieldHelp}
                    />
                  )}
                </>
              )}
                </>
              )}
            </div>
            )}

            {/* Webhook URL — only for a provider with a webhook or hybrid event, when the user provides own creds (not admin-provisioned); a polling-only provider has nothing to configure */}
            {webhookEvents && !onAdminOAuthApp && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Webhook className="h-4 w-4 text-default-secondary" />
                  <span className="content-base font-medium">{t_i18n("Webhook URL")}</span>
                </div>
                <p className="content-compact text-default-secondary">
                  {t_i18n("Use this URL to configure webhooks in {provider}. All agents using this integration will automatically receive events.", { provider: selected.name })}
                </p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px] text-accent font-mono-plex break-all select-all">
                    {baseUrl}/api/hooks/integrations/{preGeneratedId}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 w-7 p-0 shrink-0"
                    onClick={() => {
                      navigator.clipboard.writeText(`${baseUrl}/api/hooks/integrations/${preGeneratedId}`);
                      setCopiedWebhookUrl(true);
                      setTimeout(() => setCopiedWebhookUrl(false), COPY_FEEDBACK_DELAY);
                    }}
                  >
                    {copiedWebhookUrl ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
                  </Button>
                </div>
              </div>
            )}

            {/* Admin-provisioned: webhooks managed centrally, just inform the user */}
            {webhookEvents && onAdminOAuthApp && (
              <div className="flex items-center gap-2 rounded-sm bg-elevation-hover px-3 py-2">
                <Webhook className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                <p className="content-compact text-default-secondary">
                  {t_i18n("Webhooks for {provider} are managed by your administrator. Events will be delivered to your agents automatically.", { provider: selected.name })}
                </p>
              </div>
            )}

            {pollingOnlyEvents && (
              <div className="flex items-center gap-2 rounded-sm bg-elevation-hover px-3 py-2">
                <Webhook className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                <p className="content-compact text-default-secondary">
                  {t_i18n("Events from {provider} are picked up by polling. No webhook needs to be configured in {provider}.", { provider: selected.name })}
                </p>
              </div>
            )}

            {isMicrosoft && (
              <div className="flex items-center gap-2 rounded-sm bg-elevation-hover px-3 py-2">
                <Webhook className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                <p className="content-compact text-default-secondary">
                  {t_i18n("New emails and Teams messages are picked up by polling every 5 minutes. No webhook needs to be configured in Microsoft 365.")}
                </p>
              </div>
            )}

            {isSlack && (
              <div className="space-y-2 rounded-sm bg-elevation-hover px-3 py-2">
                <div className="flex items-center gap-2">
                  <Webhook className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                  <p className="content-compact text-default-secondary">
                    {t_i18n("New Slack messages are picked up by polling every 5 minutes.")}
                  </p>
                </div>
                {offersSlackEventsUrl(resolved) && (
                  <>
                    <p className="content-compact text-default-secondary">
                      {t_i18n("For real-time delivery, store the Slack app's Signing Secret in the Signing secret field of this integration, or of the Slack OAuth default when your administrator provides the app. Then set this URL as the Request URL in the app's Event Subscriptions: Slack verifies it with a signed request.")}
                    </p>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px] text-accent font-mono-plex break-all select-all">
                        {slackEventsUrl}
                      </code>
                      <CopyIconButton value={slackEventsUrl} label={t_i18n("Copy the webhook URL")} className="shrink-0" />
                    </div>
                  </>
                )}
              </div>
            )}

            {/* Company-managed toggle (admin only). Gated on the *effective* auth
                mode, not the catalog default: the backend rejects company-managed +
                OAuth (tokens are user-specific), so for dual-mode providers (e.g.
                Defender XDR, Sentinel) the toggle only appears once the admin has
                selected the app-only "API key" mode. Switching back to OAuth clears
                the flag (see the auth-mode selector) so a stale value can't 400. */}
            {isAdmin
              && effectiveAuthMode !== "oauth"
              && effectiveAuthMode !== "platform_managed" && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                <div className="flex items-center gap-2">
                  <Building2 className="h-4 w-4 text-accent" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Company-managed integration")}</p>
                    <p id="company-managed-help" className="content-compact text-default-secondary">
                      {selected.company_managed_only
                        ? t_i18n("Always company-managed: it sends from the platform's own address, so only admins create and edit it, for every user.")
                        : t_i18n("Shared with all users. Only admins can edit.")}
                    </p>
                  </div>
                </div>
                <Switch
                  aria-label={t_i18n("Company-managed integration")}
                  aria-describedby="company-managed-help"
                  checked={isCompanyManaged}
                  onCheckedChange={setIsCompanyManaged}
                  disabled={Boolean(selected.company_managed_only)}
                  className="disabled:cursor-not-allowed"
                />
              </div>
            )}

            <div className="flex items-center justify-end gap-3">
              {isOpenAPI && openapiDraft?.problem && (
                <p className="mr-auto content-compact text-default-secondary">{t_i18n(openapiDraft.problem)}</p>
              )}
              <Button variant="ghost" onClick={() => navigate("/integrations")}>{t_i18n("Cancel")}</Button>
              <Button
                onClick={handleCreate}
                disabled={!form.name.trim() || create.isPending || (isOpenAPI && !openapiDraft?.ready) || (!!selected.requires_relay && !form.relay_group_id) || hasInvalidExtraField(extraFields, selected.extra_field_inputs)}
              >
                {create.isPending ? t_i18n("Creating...") : t_i18n("Create integration")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  // `selectProvider` has run by now for every provider that reaches here.
  return <PageSkeleton cards={6} />;
}
