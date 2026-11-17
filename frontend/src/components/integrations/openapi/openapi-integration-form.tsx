/**
 * The OpenAPI-specific part of "New integration": load a spec, then choose
 * the base URL, the tool-name prefix, the authentication and the operations.
 *
 * Owns its state and reports an {@link OpenAPIDraft} upward — whether it is
 * complete, and the payload fields it contributes — so NewIntegration keeps
 * owning name, description, tags, sharing and the create call.
 */
import { Tip } from "@/components/ui/tooltip";
import { useEffect, useMemo, useState, type ElementType, type ReactNode } from "react";
import { Eye, EyeOff, FileCode2, ListChecks, RotateCcw, Server, Shield, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useFormatter } from "@/i18n";
import type { CreateIntegrationRequest, OpenAPIPreview, OpenAPIToolOverrides } from "@/types";
import {
  certificateConfig,
  credentialPayload,
  credentialProblem,
  emptyCredentials,
  prefillCredentials,
  renamePrefix,
  type OpenAPICredentials,
} from "./openapi-auth";
import { OpenAPIAuthFields } from "./openapi-auth-fields";
import { OpenAPIOperationPicker } from "./openapi-operation-picker";
import { overridesPayload } from "./openapi-overrides";
import { OpenAPISourceInput, type OpenAPISource } from "./openapi-source-input";

export interface OpenAPIDraft {
  ready: boolean;
  /** Why it is not ready (an i18n key), or null. */
  problem: string | null;
  /** The integration auth_type the chosen option stores its credential under. */
  authType: string;
  payload: Partial<CreateIntegrationRequest>;
}

const HTTP_URL = /^https?:\/\/\S+$/i;

function Section({
  icon: Icon,
  title,
  action,
  children,
}: {
  icon: ElementType;
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-default-secondary" />
        <span className="content-base font-medium">{title}</span>
        {action && <div className="ml-auto">{action}</div>}
      </div>
      {children}
    </div>
  );
}

export function OpenAPIIntegrationForm({
  onDraftChange,
  onTitle,
  onDescription,
  callbackUrl,
}: {
  onDraftChange: (draft: OpenAPIDraft) => void;
  /** Called with the API's title when a spec loads (to suggest a name). */
  onTitle: (title: string) => void;
  /** Called with the API's description when a spec loads (to suggest one). */
  onDescription?: (description: string) => void;
  callbackUrl: string;
}) {
  const { t_i18n } = useFormatter();
  const [source, setSource] = useState<OpenAPISource | null>(null);
  const [preview, setPreview] = useState<OpenAPIPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [authOptionId, setAuthOptionId] = useState("");
  const [credentials, setCredentials] = useState<OpenAPICredentials>(emptyCredentials);
  const [baseUrl, setBaseUrl] = useState("");
  const [prefix, setPrefix] = useState("");
  const [overrides, setOverrides] = useState<OpenAPIToolOverrides>({});
  const [showSecrets, setShowSecrets] = useState(false);

  const option = preview?.auth_options.find((o) => o.id === authOptionId);

  const onLoaded = (next: OpenAPIPreview, nextSource: OpenAPISource) => {
    setPreview(next);
    setSource(nextSource);
    setSelected(new Set(next.default_selection));
    setAuthOptionId(next.default_auth_option);
    setCredentials(prefillCredentials(next.auth_options.find((o) => o.id === next.default_auth_option), emptyCredentials()));
    setBaseUrl(next.default_server_url ?? "");
    setPrefix(next.tool_prefix);
    setOverrides({});
    onTitle(next.title);
    if (next.description) onDescription?.(next.description);
  };

  const chooseAuthOption = (id: string) => {
    setAuthOptionId(id);
    setCredentials((current) => prefillCredentials(preview?.auth_options.find((o) => o.id === id), current));
  };

  const draft = useMemo<OpenAPIDraft>(() => {
    const authType = option?.integration_auth_type ?? "api_key";
    if (!preview || !source) {
      return { ready: false, problem: "Load the API specification first.", authType, payload: {} };
    }
    let problem: string | null;
    if (selected.size === 0) problem = "Select at least one operation.";
    else if (!baseUrl.trim()) problem = "Enter the API base URL.";
    else if (!HTTP_URL.test(baseUrl.trim())) problem = "The API base URL must start with http:// or https://.";
    else problem = credentialProblem(option, credentials);
    const ordered = preview.operations.filter((op) => selected.has(op.key)).map((op) => op.key);
    const toolOverrides = overridesPayload(overrides);
    return {
      ready: problem === null,
      problem,
      authType,
      payload: {
        auth_type: authType,
        ...credentialPayload(option, credentials),
        extra_config: { base_url: baseUrl.trim().replace(/\/+$/, ""), ...certificateConfig(credentials) },
        openapi: {
          ...source,
          selected_operations: ordered,
          auth_option: authOptionId,
          tool_prefix: prefix,
          ...(Object.keys(toolOverrides).length > 0 ? { tool_overrides: toolOverrides } : {}),
        },
      },
    };
  }, [preview, source, selected, baseUrl, option, credentials, authOptionId, prefix, overrides]);

  useEffect(() => { onDraftChange(draft); }, [draft, onDraftChange]);

  if (!preview) {
    return (
      <Section icon={FileCode2} title={t_i18n("API specification")}>
        <OpenAPISourceInput onLoaded={onLoaded} />
      </Section>
    );
  }

  return (
    <div className="space-y-6">
      <Section icon={FileCode2} title={t_i18n("API specification")}>
        <div className="flex flex-wrap items-center gap-2 rounded-sm bg-elevation-hover p-3">
          <div className="min-w-0 flex-1">
            <p className="truncate content-base font-medium">{preview.title}</p>
            <p className="truncate text-[11px] text-default-secondary">
              {source?.url ?? source?.filename}
            </p>
          </div>
          <Badge variant="outline" className="content-caption">
            {preview.spec_version.startsWith("2") ? `Swagger ${preview.spec_version}` : `OpenAPI ${preview.spec_version}`}
          </Badge>
          <Badge variant="outline" className="content-caption uppercase">{preview.document_format}</Badge>
          {preview.api_version && <Badge variant="outline" className="content-caption">v{preview.api_version}</Badge>}
          <Badge variant="cyan" className="content-caption">
            {t_i18n("{count} operations", { count: preview.operation_count })}
          </Badge>
          <Button variant="ghost" size="sm" className="h-7 gap-1 content-compact" onClick={() => { setPreview(null); setSource(null); }}>
            <RotateCcw className="h-3 w-3" />
            {t_i18n("Change")}
          </Button>
        </div>
        {preview.warnings.length > 0 && (
          <div className="space-y-1 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 p-3">
            {preview.warnings.map((warning) => (
              <p key={warning} className="flex items-start gap-1.5 content-caption text-feedback-warning-primary">
                <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                {warning}
              </p>
            ))}
          </div>
        )}
      </Section>

      <Section icon={Server} title={t_i18n("Connection")}>
        <div className="grid gap-3 sm:grid-cols-[1fr_12rem]">
          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("API base URL")}</label>
            <Input aria-label={t_i18n("API base URL")} className="font-mono-plex content-compact" placeholder="https://api.example.com/v1" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
            {preview.servers.length > 1 && (
              <div className="flex flex-wrap gap-1 pt-1">
                {preview.servers.map((server) => {
                  const chip = (
                    <button
                      key={server.url}
                      type="button"
                      onClick={() => setBaseUrl(server.url)}
                      className="rounded-sm border border-elevation-subtle-soft px-1.5 py-0.5 font-mono-plex content-caption text-default-secondary hover:text-default-primary"
                    >
                      {server.name ? `${server.name} · ${server.url}` : server.url}
                    </button>
                  );
                  return server.description ? (
                    <Tip key={server.url} content={server.description}>
                      {chip}
                    </Tip>
                  ) : (
                    chip
                  );
                })}
              </div>
            )}
          </div>
          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Tool name prefix")}</label>
            <Input aria-label={t_i18n("Tool name prefix")} className="font-mono-plex content-compact" value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder={t_i18n("none")} />
          </div>
        </div>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Every call goes to the base URL. The prefix starts every tool name, so agents can tell this API's tools apart.")}
        </p>
      </Section>

      <Section
        icon={Shield}
        title={t_i18n("Credentials")}
        action={
          <Button variant="ghost" size="sm" className="h-6 px-2 content-compact" onClick={() => setShowSecrets(!showSecrets)}>
            {showSecrets ? <EyeOff className="mr-1 h-3 w-3" /> : <Eye className="mr-1 h-3 w-3" />}
            {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
          </Button>
        }
      >
        <OpenAPIAuthFields
          options={preview.auth_options}
          value={authOptionId}
          onChange={chooseAuthOption}
          credentials={credentials}
          onCredentialsChange={setCredentials}
          showSecrets={showSecrets}
          callbackUrl={callbackUrl}
        />
      </Section>

      <Section icon={ListChecks} title={t_i18n("Operations")}>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Each selected operation becomes one tool. Deprecated operations start switched off; you can change the selection later from the integration's API Spec tab.")}
        </p>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Use the pencil to rename a tool or rewrite its description: agents choose tools by these.")}
        </p>
        <OpenAPIOperationPicker
          operations={preview.operations}
          tags={preview.tags}
          selected={selected}
          onChange={setSelected}
          toolName={(op) => renamePrefix(op.tool_name, preview.tool_prefix, prefix)}
          overrides={overrides}
          onOverridesChange={setOverrides}
        />
      </Section>
    </div>
  );
}
