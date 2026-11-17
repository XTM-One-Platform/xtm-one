// SPDX-License-Identifier: Apache-2.0

/**
 * "API Spec" tab of an OpenAPI integration: where its tools come from.
 *
 * Shows the imported document (source, version, authentication), lets a
 * manager change which operations are tools without re-importing, refresh a
 * URL-sourced spec (new operations stay off until selected), replace the spec
 * with a new URL, file or archive, edit the OAuth endpoints the spec
 * pre-filled, and set the client certificate presented on every call.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { FileBadge, FileCode2, Link2, RefreshCw, Replace, Save, ShieldCheck, Trash2, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { authOptionLabel } from "@/components/integrations/openapi/openapi-auth";
import { ClientCertificateFields, type CertificateDraft } from "@/components/integrations/openapi/openapi-certificate-fields";
import { OpenAPIOperationPicker } from "@/components/integrations/openapi/openapi-operation-picker";
import {
  overridesFromOperations,
  overridesPayload,
  sameOverrides,
} from "@/components/integrations/openapi/openapi-overrides";
import { OpenAPISourceInput, type OpenAPISource } from "@/components/integrations/openapi/openapi-source-input";
import {
  useDeleteClientCertificate,
  useIntegrationOpenAPISpec,
  useRefreshOpenAPISpec,
  useReplaceOpenAPISpec,
  useSetClientCertificate,
  useUpdateIntegration,
  useUpdateOpenAPIOperations,
} from "@/hooks/api/useIntegrationApi";
import { useAuth } from "@/hooks/useAuth";
import { canManageEntity } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import { useFormatter } from "@/i18n";
import type { Integration, OpenAPIAuthKind, OpenAPIPreview, OpenAPISpec, OpenAPIToolOverrides } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

function selectedKeys(spec: OpenAPISpec): Set<string> {
  return new Set(spec.operations.filter((op) => op.selected).map((op) => op.key));
}

export function OpenAPISpecTab({ integration }: { integration: Integration }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const canManage = !integration.is_builtin && canManageEntity(integration, user?.id, isAdmin);
  const { data: spec, isLoading, error } = useIntegrationOpenAPISpec(integration.id);
  const updateOperations = useUpdateOpenAPIOperations();
  const refresh = useRefreshOpenAPISpec();
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [overrides, setOverrides] = useState<OpenAPIToolOverrides>({});
  const [replaceOpen, setReplaceOpen] = useState(false);
  // The saved selection and tool names the draft started from. The spec is
  // refetched after any change on this page (a certificate saved, the OAuth
  // endpoints edited): only a change to what the draft edits resets it.
  const adoptedBaseline = useRef<string | null>(null);

  useEffect(() => {
    if (!spec) return;
    const baseline = JSON.stringify([
      [...selectedKeys(spec)].sort(),
      overridesFromOperations(spec.operations),
    ]);
    if (baseline === adoptedBaseline.current) return;
    adoptedBaseline.current = baseline;
    setSelection(selectedKeys(spec));
    setOverrides(overridesFromOperations(spec.operations));
  }, [spec]);

  const dirty = useMemo(() => {
    if (!spec) return false;
    const current = selectedKeys(spec);
    const selectionChanged = current.size !== selection.size || [...selection].some((key) => !current.has(key));
    return selectionChanged || !sameOverrides(overrides, overridesFromOperations(spec.operations));
  }, [spec, selection, overrides]);

  if (isLoading) return <PageSkeleton cards={2} />;
  if (error || !spec) {
    return (
      <Card>
        <CardContent className="py-8 text-center content-base text-default-secondary">
          {error instanceof Error ? error.message : t_i18n("No API specification is attached to this integration.")}
        </CardContent>
      </Card>
    );
  }

  const saveSelection = () => {
    updateOperations.mutate(
      { integrationId: integration.id, selected_operations: [...selection], tool_overrides: overridesPayload(overrides) },
      {
        onSuccess: () => toast({ title: t_i18n("Operations updated"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("Could not update the operations"), description: err.message, variant: "error" }),
      },
    );
  };

  const runRefresh = () => {
    refresh.mutate(integration.id, {
      onSuccess: (result) => {
        const parts: string[] = [];
        if (result.added_operations.length > 0) {
          parts.push(t_i18n("{count, plural, one {# new operation} other {# new operations}} available, not enabled.", { count: result.added_operations.length }));
        }
        if (result.removed_operations.length > 0) {
          parts.push(t_i18n("{count, plural, one {# selected operation no longer exists.} other {# selected operations no longer exist.}}", { count: result.removed_operations.length }));
        }
        toast({
          title: result.changed ? t_i18n("Spec refreshed") : t_i18n("The spec has not changed"),
          description: parts.join(" ") || undefined,
          variant: "success",
        });
      },
    });
  };

  // The address the spec was fetched from, as the integration's author typed it.
  const sourceHref = safeHttpUrl(spec.source_url);
  const versionLabel = spec.spec_version.startsWith("2") ? `Swagger ${spec.spec_version}` : `OpenAPI ${spec.spec_version}`;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex flex-wrap items-center gap-2 content-base">
            <FileCode2 className="h-4 w-4 text-accent" />
            {spec.title}
            <Badge variant="outline" className="content-caption">{versionLabel}</Badge>
            <Badge variant="outline" className="content-caption uppercase">{spec.document_format}</Badge>
            {spec.api_version && <Badge variant="outline" className="content-caption">v{spec.api_version}</Badge>}
            {canManage && (
              <div className="ml-auto flex gap-2">
                {spec.source_type === "url" && (
                  <Button variant="secondary" size="sm" className="gap-1.5" onClick={runRefresh} disabled={refresh.isPending}>
                    <RefreshCw className={refresh.isPending ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
                    {t_i18n("Refresh from URL")}
                  </Button>
                )}
                <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => setReplaceOpen(true)}>
                  <Replace className="h-3.5 w-3.5" />
                  {t_i18n("Replace spec")}
                </Button>
              </div>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {spec.description && <p className="content-compact text-default-secondary">{spec.description}</p>}
          <div className="grid gap-3 content-compact sm:grid-cols-2 lg:grid-cols-4">
            <Fact label={t_i18n("Source")}>
              {spec.source_type === "url" && spec.source_url ? (
                sourceHref ? (
                  <a href={sourceHref} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 break-all text-accent hover:underline">
                    <Link2 className="h-3 w-3 shrink-0" />
                    {spec.source_url}
                  </a>
                ) : (
                  <span className="break-all">{spec.source_url}</span>
                )
              ) : (
                <span>{spec.source_filename ?? t_i18n("Uploaded file")}</span>
              )}
            </Fact>
            <Fact label={t_i18n("Authentication")}>
              <span className="inline-flex items-center gap-1">
                <ShieldCheck className="h-3 w-3 text-feedback-success-primary" />
                {spec.auth_option ? authOptionLabel(spec.auth_option, t_i18n) : t_i18n("None")}
              </span>
            </Fact>
            <Fact label={t_i18n("Operations exposed")}>
              {t_i18n("{selected} of {total}", { selected: spec.selected_count, total: spec.supported_count })}
            </Fact>
            <Fact label={t_i18n("Last imported")}>
              {spec.refreshed_at ? formatTimeAgo(intl, spec.refreshed_at) : "—"}
            </Fact>
          </div>
          {spec.tool_prefix && (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("Tool names start with")} <code className="font-mono-plex text-default-primary">{spec.tool_prefix}_</code>
            </p>
          )}
          {spec.warnings.length > 0 && (
            <div className="space-y-1 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 p-3">
              {spec.warnings.map((warning) => (
                <p key={warning} className="flex items-start gap-1.5 content-caption text-feedback-warning-primary">
                  <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                  {warning}
                </p>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {["oauth", "client_credentials", "oauth_password"].includes(integration.auth_type) && (
        <OAuthEndpointsCard integration={integration} canManage={canManage} kind={spec.auth_option?.kind} />
      )}

      <ClientCertificateCard integration={integration} spec={spec} canManage={canManage} />

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 content-base">
            {t_i18n("Operations")}
            {canManage && (
              <Button size="sm" className="ml-auto gap-1.5" onClick={saveSelection} disabled={!dirty || updateOperations.isPending}>
                <Save className="h-3.5 w-3.5" />
                {updateOperations.isPending ? t_i18n("Saving...") : t_i18n("Save changes")}
              </Button>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <OpenAPIOperationPicker
            operations={spec.operations}
            tags={spec.tags}
            selected={selection}
            onChange={setSelection}
            disabled={!canManage}
            overrides={overrides}
            onOverridesChange={setOverrides}
          />
        </CardContent>
      </Card>

      {canManage && (
        <ReplaceSpecDialog
          open={replaceOpen}
          onOpenChange={setReplaceOpen}
          integration={integration}
          spec={spec}
        />
      )}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{label}</p>
      <div className="content-compact">{children}</div>
    </div>
  );
}

/** The endpoints each OAuth2 option uses, and the card's title. */
const OAUTH_CARDS: Partial<Record<OpenAPIAuthKind, { title: string; start?: string; token: boolean; signIn: boolean }>> = {
  oauth2_authorization_code: { title: "OAuth2 authorization code", start: "Authorization URL", token: true, signIn: true },
  oauth2_implicit: { title: "OAuth2 implicit", start: "Authorization URL", token: false, signIn: true },
  oauth2_device: { title: "OAuth2 device authorization", start: "Device authorization URL", token: true, signIn: true },
  oauth2_client_credentials: { title: "OAuth2 client credentials", token: true, signIn: false },
  oauth2_password: { title: "OAuth2 password", token: true, signIn: false },
};

function OAuthEndpointsCard({ integration, canManage, kind }: { integration: Integration; canManage: boolean; kind?: OpenAPIAuthKind }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateIntegration();
  const [authorizeUrl, setAuthorizeUrl] = useState(integration.authorize_url ?? "");
  const [tokenUrl, setTokenUrl] = useState(integration.token_url ?? "");
  const [scopes, setScopes] = useState(integration.scopes ?? "");
  const card =
    (kind && OAUTH_CARDS[kind]) ??
    (integration.auth_type === "oauth" ? OAUTH_CARDS.oauth2_authorization_code! : OAUTH_CARDS.oauth2_client_credentials!);
  const dirty =
    (card.token && tokenUrl !== (integration.token_url ?? "")) ||
    scopes !== (integration.scopes ?? "") ||
    (!!card.start && authorizeUrl !== (integration.authorize_url ?? ""));

  const save = () => {
    update.mutate(
      {
        id: integration.id,
        scopes: scopes.trim(),
        ...(card.token ? { token_url: tokenUrl.trim() } : {}),
        ...(card.start ? { authorize_url: authorizeUrl.trim() } : {}),
      },
      { onSuccess: () => toast({ title: t_i18n("OAuth endpoints saved"), variant: "success" }) },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          {t_i18n(card.title)}
          {canManage && (
            <Button size="sm" className="ml-auto gap-1.5" onClick={save} disabled={!dirty || update.isPending}>
              <Save className="h-3.5 w-3.5" />
              {t_i18n("Save")}
            </Button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {card.start && (
          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n(card.start)}</label>
            <Input aria-label={t_i18n(card.start)} className="font-mono-plex content-compact" value={authorizeUrl} onChange={(e) => setAuthorizeUrl(e.target.value)} disabled={!canManage} />
          </div>
        )}
        {card.token && (
          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Token URL")}</label>
            <Input aria-label={t_i18n("Token URL")} className="font-mono-plex content-compact" value={tokenUrl} onChange={(e) => setTokenUrl(e.target.value)} disabled={!canManage} />
          </div>
        )}
        <div className="space-y-1">
          <label className="content-compact text-default-secondary">{t_i18n("Scopes (space-separated)")}</label>
          <Input aria-label={t_i18n("Scopes (space-separated)")} className="font-mono-plex content-compact" value={scopes} onChange={(e) => setScopes(e.target.value)} disabled={!canManage} />
        </div>
        {card.signIn && (
          <p className="text-[11px] text-default-secondary">
            {t_i18n("After changing the scopes, click Authorize again so the new token carries them.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ClientCertificateCard({ integration, spec, canManage }: { integration: Integration; spec: OpenAPISpec; canManage: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const setCertificate = useSetClientCertificate();
  const removeCertificate = useDeleteClientCertificate();
  const [editing, setEditing] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);
  const [draft, setDraft] = useState<CertificateDraft>({ certificate: "", privateKey: "", passphrase: "" });
  const certificate = spec.client_certificate;
  const isCredential = spec.auth_option?.kind === "mutual_tls";

  const save = () => {
    setCertificate.mutate(
      {
        integrationId: integration.id,
        certificate: draft.certificate,
        private_key: draft.privateKey,
        ...(draft.passphrase ? { passphrase: draft.passphrase } : {}),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Client certificate saved"), variant: "success" });
          setEditing(false);
          setDraft({ certificate: "", privateKey: "", passphrase: "" });
        },
        onError: (err) => toast({ title: t_i18n("The certificate was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const remove = () => {
    removeCertificate.mutate(integration.id, {
      onSuccess: () => toast({ title: t_i18n("Client certificate removed"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("The certificate was not removed"), description: err.message, variant: "error" }),
    });
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <FileBadge className="h-4 w-4 text-accent" />
          {t_i18n("Client certificate (mutual TLS)")}
          {canManage && !editing && (
            <div className="ml-auto flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
                {certificate ? t_i18n("Replace") : t_i18n("Add a certificate")}
              </Button>
              {certificate && !isCredential && (
                <Button variant="ghost" size="sm" className="gap-1.5" onClick={remove} disabled={removeCertificate.isPending}>
                  <Trash2 className="h-3.5 w-3.5" />
                  {t_i18n("Remove")}
                </Button>
              )}
            </div>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {certificate ? (
          <div className="grid gap-3 content-compact sm:grid-cols-2">
            <Fact label={t_i18n("Certificate subject")}><span className="break-all font-mono-plex">{certificate.subject}</span></Fact>
            <Fact label={t_i18n("Issuer")}><span className="break-all font-mono-plex">{certificate.issuer}</span></Fact>
            <Fact label={t_i18n("Valid until")}>
              <span className={certificate.expired ? "text-feedback-error-primary" : certificate.expires_soon ? "text-feedback-warning-primary" : undefined}>
                {intl.formatDate(new Date(certificate.not_after), { dateStyle: "medium" })}
                {certificate.expired ? ` — ${t_i18n("expired")}` : certificate.expires_soon ? ` — ${t_i18n("expires soon")}` : ""}
              </span>
            </Fact>
            <Fact label={t_i18n("SHA-256 fingerprint")}><span className="break-all font-mono-plex content-caption">{certificate.fingerprint_sha256}</span></Fact>
            {(certificate.expired || certificate.expires_soon) && (
              <p className={`sm:col-span-2 ${certificate.expired ? "text-feedback-error-primary" : "text-feedback-warning-primary"}`}>
                {certificate.expired
                  ? t_i18n("This certificate has expired: calls are refused until it is replaced.")
                  : t_i18n("This certificate expires within 30 days: replace it before then, or calls will stop.")}
              </p>
            )}
          </div>
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n("No client certificate. Add one when the API asks for mutual TLS: it is presented on every call and every token request.")}
          </p>
        )}
        {editing && (
          <div className="space-y-3 rounded-sm border border-elevation-subtle-soft p-3">
            <div className="flex justify-end">
              <Button variant="ghost" size="sm" className="h-6 px-2 content-compact" onClick={() => setShowSecrets(!showSecrets)}>
                {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
              </Button>
            </div>
            <ClientCertificateFields value={draft} onChange={setDraft} showSecrets={showSecrets} />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setEditing(false)}>{t_i18n("Cancel")}</Button>
              <Button onClick={save} disabled={!draft.certificate.trim() || !draft.privateKey.trim() || setCertificate.isPending}>
                {setCertificate.isPending ? t_i18n("Saving...") : t_i18n("Save certificate")}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReplaceSpecDialog({
  open,
  onOpenChange,
  integration,
  spec,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  integration: Integration;
  spec: OpenAPISpec;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const replace = useReplaceOpenAPISpec();
  const [preview, setPreview] = useState<OpenAPIPreview | null>(null);
  const [source, setSource] = useState<OpenAPISource | null>(null);
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [authOptionId, setAuthOptionId] = useState("");

  // Only options storing the credential this integration already has.
  const compatible = (preview?.auth_options ?? []).filter(
    (o) => o.supported && o.integration_auth_type === integration.auth_type,
  );

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setPreview(null);
      setSource(null);
    }
  };

  const onLoaded = (next: OpenAPIPreview, nextSource: OpenAPISource) => {
    setPreview(next);
    setSource(nextSource);
    const previous = selectedKeys(spec);
    const supported = new Set(next.operations.filter((op) => op.supported).map((op) => op.key));
    const kept = [...previous].filter((key) => supported.has(key));
    setSelection(new Set(kept.length > 0 ? kept : next.default_selection));
    const options = next.auth_options.filter((o) => o.supported && o.integration_auth_type === integration.auth_type);
    const same = options.find((o) => o.id === spec.auth_option?.id);
    setAuthOptionId(same?.id ?? options[0]?.id ?? "");
  };

  const confirm = () => {
    if (!preview || !source || !authOptionId) return;
    replace.mutate(
      {
        integrationId: integration.id,
        ...source,
        tool_prefix: spec.tool_prefix,
        auth_option: authOptionId,
        selected_operations: preview.operations.filter((op) => selection.has(op.key)).map((op) => op.key),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Spec replaced"), variant: "success" });
          close(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("Replace the API specification")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Load the new document, check the operations to expose, then replace. The credentials stay as they are.")}
          </DialogDescription>
        </DialogHeader>
        {!preview ? (
          <OpenAPISourceInput onLoaded={onLoaded} initialUrl={spec.source_url ?? undefined} />
        ) : (
          <div className="space-y-4">
            <p className="content-base font-medium">
              {preview.title} <span className="content-compact text-default-secondary">· {preview.spec_version} · {preview.document_format.toUpperCase()}</span>
            </p>
            {compatible.length === 0 ? (
              <div className="rounded-sm border border-feedback-error-primary/20 bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary">
                {t_i18n("This document offers no authentication using the kind of credential this integration stores. Create a new integration for it instead.")}
              </div>
            ) : (
              <div className="space-y-1.5">
                <label className="content-compact text-default-secondary">{t_i18n("Authentication")}</label>
                <Select value={authOptionId} onValueChange={setAuthOptionId}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {compatible.map((o) => (
                      <SelectItem key={o.id} value={o.id}>{authOptionLabel(o, t_i18n)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <OpenAPIOperationPicker operations={preview.operations} tags={preview.tags} selected={selection} onChange={setSelection} />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => { setPreview(null); setSource(null); }}>{t_i18n("Back")}</Button>
              <Button onClick={confirm} disabled={compatible.length === 0 || selection.size === 0 || replace.isPending}>
                {replace.isPending ? t_i18n("Replacing...") : t_i18n("Replace spec")}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
