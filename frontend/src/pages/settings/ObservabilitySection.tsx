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
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  ExternalLink,
  Globe,
  HardDrive,
  Key,
  Pencil,
  Plug2,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Input } from "@/components/ui/input";
import { SecretField } from "@/components/ui/secret-field";
import {
  SettingsField,
  SettingsFieldGrid,
  SettingsGroupDescription,
  SettingsGroupTitle,
  SettingsSwitchField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import {
  useLangfuseConfig,
  useObservabilityConfig,
  useTestLangfuse,
  useUpdateLangfuseConfig,
  useUpdateObservabilityConfig,
} from "@/hooks/api/useSettingsApi";
import { LANGFUSE_DEFAULT_HOST } from "@/lib/constants";
import {
  AUDIT_LOG_RETENTION_DEFAULT_DAYS,
  MAX_AUDIT_LOG_RETENTION_DAYS,
  MIN_AUDIT_LOG_RETENTION_DAYS,
  parseAuditLogRetentionDays,
} from "@/lib/audit-log-retention";
import {
  COORDINATION_RETENTION_DEFAULTS,
  COORDINATION_RETENTION_KEYS,
  MAX_COORDINATION_RETENTION_DAYS,
  MIN_COORDINATION_RETENTION_DAYS,
  parseCoordinationRetentionDays,
  type CoordinationRetention,
  type CoordinationRetentionKey,
} from "@/lib/coordination-retention";
import {
  MAX_TRACE_RETENTION_DAYS,
  MIN_TRACE_RETENTION_DAYS,
  TRACE_RETENTION_DEFAULT_DAYS,
  parseTraceRetentionDays,
} from "@/lib/trace-retention";
import { cn, formatDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

function coordinationInputsFrom(values: CoordinationRetention): Record<CoordinationRetentionKey, string> {
  return Object.fromEntries(COORDINATION_RETENTION_KEYS.map((key) => [key, String(values[key])])) as Record<
    CoordinationRetentionKey,
    string
  >;
}

// Keeps the current state object when every window is unchanged, so an effect that re-syncs from a fresh
// config object (a refetch, a re-render) does not schedule another render and loop.
function keepCoordinationInputs(
  current: Record<CoordinationRetentionKey, string>,
  next: Record<CoordinationRetentionKey, string>,
): Record<CoordinationRetentionKey, string> {
  return COORDINATION_RETENTION_KEYS.every((key) => current[key] === next[key]) ? current : next;
}

export function ObservabilitySection() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { toast } = useToast();

  const { data: config } = useObservabilityConfig();
  const { data: langfuseConfig } = useLangfuseConfig();

  const updateObservability = useUpdateObservabilityConfig();
  const updateLangfuse = useUpdateLangfuseConfig();
  const testLangfuseConnection = useTestLangfuse();

  const [editing, setEditing] = useState(false);

  const [traceStorageEnabled, setTraceStorageEnabled] = useState(true);
  // Kept as typed, never clamped on a keystroke: a value out of the bounds shows an inline error and blocks Save.
  const [traceRetentionInput, setTraceRetentionInput] = useState(String(TRACE_RETENTION_DEFAULT_DAYS));
  const traceRetentionRef = useRef<HTMLInputElement | null>(null);
  const traceRetentionDays = parseTraceRetentionDays(traceRetentionInput);
  const traceRetentionInvalid = traceRetentionDays === null;
  const [traceStorePayloads, setTraceStorePayloads] = useState(true);
  // Kept as typed, never clamped: a value out of the bounds shows an inline error and blocks Save.
  const [auditLogRetentionInput, setAuditLogRetentionInput] = useState(String(AUDIT_LOG_RETENTION_DEFAULT_DAYS));
  const auditLogRetentionRef = useRef<HTMLInputElement | null>(null);
  const auditLogRetentionDays = parseAuditLogRetentionDays(auditLogRetentionInput);
  const auditLogRetentionInvalid = auditLogRetentionDays === null;
  // Kept as typed, never clamped: a value out of 0 to 3650 shows an inline error and blocks Save.
  const [coordinationInputs, setCoordinationInputs] = useState<Record<CoordinationRetentionKey, string>>(
    () => coordinationInputsFrom(COORDINATION_RETENTION_DEFAULTS),
  );
  const coordinationRefs = useRef<Partial<Record<CoordinationRetentionKey, HTMLInputElement | null>>>({});
  const coordinationDays = Object.fromEntries(
    COORDINATION_RETENTION_KEYS.map((key) => [key, parseCoordinationRetentionDays(coordinationInputs[key])]),
  ) as Record<CoordinationRetentionKey, number | null>;
  const coordinationRetention = { ...COORDINATION_RETENTION_DEFAULTS, ...(config?.coordination_retention ?? {}) };
  const coordinationLabels: Record<CoordinationRetentionKey, { title: string; label: string; helper: string }> = {
    work_item_resolved_days: {
      title: t_i18n("Closed work items"),
      label: t_i18n("Closed work items (days)"),
      helper: t_i18n("Done and cancelled work items are deleted this many days after their last update."),
    },
    shared_state_resolved_days: {
      title: t_i18n("Resolved shared state"),
      label: t_i18n("Resolved shared state (days)"),
      helper: t_i18n("Resolved shared state entries are deleted this many days after their last update."),
    },
    work_item_abandoned_days: {
      title: t_i18n("Unclaimed open work items"),
      label: t_i18n("Unclaimed open work items (days)"),
      helper: t_i18n("Open work items nobody picks up are cancelled this many days after their last activity."),
    },
    shared_state_archive_days: {
      title: t_i18n("Archived shared state items"),
      label: t_i18n("Archived shared state items (days)"),
      helper: t_i18n("List items moved to an archive entry are dropped this many days after they were archived."),
    },
    workset_resolved_days: {
      title: t_i18n("Resolved worksets"),
      label: t_i18n("Resolved worksets (days)"),
      helper: t_i18n("Resolved worksets are deleted with their documents this many days after they were resolved."),
    },
  };
  const retentionText = (days: number) =>
    days === 0 ? t_i18n("Kept forever") : t_i18n("{count, plural, one {# day} other {# days}}", { count: days });

  const [langfuseEnabled, setLangfuseEnabled] = useState(false);
  const [langfusePublicKey, setLangfusePublicKey] = useState("");
  const [langfuseSecretKey, setLangfuseSecretKey] = useState("");
  const [langfuseHost, setLangfuseHost] = useState(LANGFUSE_DEFAULT_HOST);

  const isClickHouseProvider = config?.trace_store_provider === "clickhouse";
  const isTraceStoreHealthy = config?.trace_store_health_status === "healthy";
  const isLangfuseEnvManaged = langfuseConfig?.is_from_env ?? false;

  const traceStoreProviderLabel = config?.trace_store_provider === "clickhouse" ? "ClickHouse" : "PostgreSQL";
  const traceStoreDescription =
    config?.trace_store_provider === "clickhouse"
      ? t_i18n("Persist traces in local ClickHouse")
      : t_i18n("Persist traces in local PostgreSQL");

  const isSaving = updateObservability.isPending || updateLangfuse.isPending;

  useEffect(() => {
    if (!config || editing) return;
    setTraceStorageEnabled(config.trace_storage_enabled);
    setTraceRetentionInput(String(config.trace_retention_days));
    setTraceStorePayloads(config.trace_store_payloads);
    setAuditLogRetentionInput(String(config.audit_log_retention_days));
    const next = coordinationInputsFrom({ ...COORDINATION_RETENTION_DEFAULTS, ...(config.coordination_retention ?? {}) });
    setCoordinationInputs((current) => keepCoordinationInputs(current, next));
  }, [config, editing]);

  useEffect(() => {
    if (!langfuseConfig || editing) return;
    setLangfuseEnabled(langfuseConfig.enabled);
    setLangfuseHost(langfuseConfig.host || LANGFUSE_DEFAULT_HOST);
    setLangfusePublicKey("");
    setLangfuseSecretKey("");
  }, [langfuseConfig, editing]);

  const startEditing = () => {
    if (config) {
      setTraceStorageEnabled(config.trace_storage_enabled);
      setTraceRetentionInput(String(config.trace_retention_days));
      setTraceStorePayloads(config.trace_store_payloads);
      setAuditLogRetentionInput(String(config.audit_log_retention_days));
      const next = coordinationInputsFrom({ ...COORDINATION_RETENTION_DEFAULTS, ...(config.coordination_retention ?? {}) });
      setCoordinationInputs((current) => keepCoordinationInputs(current, next));
    }

    if (langfuseConfig) {
      setLangfuseEnabled(langfuseConfig.enabled);
      setLangfuseHost(langfuseConfig.host || LANGFUSE_DEFAULT_HOST);
    }

    setLangfusePublicKey("");
    setLangfuseSecretKey("");
    setEditing(true);
  };

  const handleCancel = () => {
    setEditing(false);
    setLangfusePublicKey("");
    setLangfuseSecretKey("");
  };

  const handleSave = async () => {
    if (traceRetentionDays === null) {
      traceRetentionRef.current?.focus();
      return;
    }
    if (auditLogRetentionDays === null) {
      auditLogRetentionRef.current?.focus();
      return;
    }
    const invalidCoordination = COORDINATION_RETENTION_KEYS.find((key) => coordinationDays[key] === null);
    if (invalidCoordination) {
      coordinationRefs.current[invalidCoordination]?.focus();
      return;
    }
    try {
      await updateObservability.mutateAsync({
        trace_storage_enabled: traceStorageEnabled,
        trace_retention_days: traceRetentionDays,
        trace_store_payloads: traceStorePayloads,
        audit_log_retention_days: auditLogRetentionDays,
        coordination_retention: coordinationDays as CoordinationRetention,
      });

      if (!isLangfuseEnvManaged) {
        await updateLangfuse.mutateAsync({
          enabled: langfuseEnabled,
          public_key: langfusePublicKey || undefined,
          secret_key: langfuseSecretKey || undefined,
          host: langfuseHost || LANGFUSE_DEFAULT_HOST,
        });
      }

      setEditing(false);
      setLangfusePublicKey("");
      setLangfuseSecretKey("");
      toast({ title: t_i18n("Observability settings saved"), variant: "success" });
    } catch {
      toast({ title: t_i18n("Failed to save observability settings"), variant: "error" });
    }
  };

  return (
    <Card>
      <CardContent className="pt-6 space-y-6">
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <p className="content-base font-medium">{t_i18n("Observability Setup")}</p>
            <p className="content-compact text-default-secondary">
              {t_i18n("Configure local traces and optional Langfuse export in one place.")}
            </p>
          </div>
          {!editing ? (
            isAdmin ? (
              <Button variant="secondary" size="sm" className="gap-1.5 shrink-0 ml-4" onClick={startEditing}>
                <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
              </Button>
            ) : null
          ) : (
            <div className="flex gap-2 shrink-0 ml-4">
              <Button variant="secondary" onClick={handleCancel}>{t_i18n("Cancel")}</Button>
              <Button onClick={handleSave} disabled={isSaving} aria-busy={isSaving || undefined}>
                {isSaving && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Save")}
              </Button>
            </div>
          )}
        </div>

        {!editing ? (
          <div className="space-y-6">
            <div className="space-y-3">
              <p className="content-compact font-medium text-default-secondary">{t_i18n("Local tracing")}</p>

              {config?.aggregation_stale ? (
                <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3 content-compact text-feedback-warning-tertiary">
                  <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>
                    {t_i18n("Daily aggregation is stale (>48h).")}
                    {config.last_aggregation_at
                      ? " " + t_i18n("Last successful run: {time}.", { time: formatDateTime(config.last_aggregation_at) })
                      : " " + t_i18n("No successful run has been recorded yet.")}
                  </span>
                </div>
              ) : null}

              {config?.pricing_stale ? (
                <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3 content-compact text-feedback-warning-tertiary">
                  <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>
                    {config.pricing_staleness_days !== null
                      ? t_i18n("Pricing last updated: {count, plural, one {# day} other {# days}} ago.", { count: config.pricing_staleness_days })
                      : t_i18n("Pricing has not been updated yet.")}
                  </span>
                </div>
              ) : null}

              <div className="rounded-sm bg-elevation-default p-4 space-y-2">
                <p className="content-base font-medium">{t_i18n("Models without pricing")}</p>
                {config?.models_without_pricing && config.models_without_pricing.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {config.models_without_pricing.map((model) => (
                      <Badge key={model} variant="outline" className="content-caption">
                        {model}
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="content-compact text-default-secondary">{t_i18n("All observed models currently resolve to pricing data.")}</p>
                )}
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <HardDrive className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Trace Storage")} ({traceStoreProviderLabel})</p>
                    <p className="content-compact text-default-secondary">{traceStoreDescription}</p>
                    {isClickHouseProvider ? (
                      <p className={isTraceStoreHealthy ? "content-compact text-feedback-success-primary" : "content-compact text-feedback-error-primary"}>
                        {config?.trace_store_health_message}
                      </p>
                    ) : null}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {isClickHouseProvider ? (
                    <Badge
                      variant={isTraceStoreHealthy ? "success" : "outline"}
                      className={isTraceStoreHealthy ? "content-caption" : "content-caption border-feedback-error-primary/40 text-feedback-error-primary"}
                    >
                      {isTraceStoreHealthy ? (
                        <>
                          <CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Connected")}
                        </>
                      ) : (
                        <>
                          <AlertTriangle className="h-3 w-3 mr-1" /> {t_i18n("Error")}
                        </>
                      )}
                    </Badge>
                  ) : null}
                  <Badge variant={config?.trace_storage_enabled ? "success" : "outline"} className="content-caption">
                    {config?.trace_storage_enabled ? <><CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Enabled")}</> : t_i18n("Disabled")}
                  </Badge>
                </div>
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Clock3 className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Retention")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("{count, plural, one {# day} other {# days}}", { count: config?.trace_retention_days ?? 90 })}</p>
                  </div>
                </div>
                <Badge variant="outline" className="content-caption">{t_i18n("{count, plural, one {# day} other {# days}}", { count: config?.trace_retention_days ?? 90 })}</Badge>
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Database className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Payload Storage")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("Store full input/output payloads")}</p>
                  </div>
                </div>
                <Badge variant={config?.trace_store_payloads ? "success" : "outline"} className="content-caption">
                  {config?.trace_store_payloads ? t_i18n("Storing") : t_i18n("Not storing")}
                </Badge>
              </div>
            </div>

            <div className="space-y-3 border-t border-elevation-subtle-soft pt-4">
              <p className="content-compact font-medium text-default-secondary">{t_i18n("Audit log")}</p>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Clock3 className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Audit log retention")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("Older entries are deleted automatically, every hour.")}</p>
                  </div>
                </div>
                <Badge variant="outline" className="content-caption" data-testid="observability-audit-log-retention">
                  {t_i18n("{count, plural, one {# day} other {# days}}", { count: config?.audit_log_retention_days ?? AUDIT_LOG_RETENTION_DEFAULT_DAYS })}
                </Badge>
              </div>
            </div>

            <div className="space-y-3 border-t border-elevation-subtle-soft pt-4">
              <p className="content-compact font-medium text-default-secondary">{t_i18n("Coordination retention")}</p>

              <div className="space-y-3" data-testid="observability-coordination-retention">
                {COORDINATION_RETENTION_KEYS.map((key) => (
                  <div key={key} className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-4">
                    <div className="flex items-center gap-3 min-w-0">
                      <Clock3 className="h-5 w-5 shrink-0 text-default-secondary" />
                      <div className="min-w-0">
                        <p className="content-base font-medium">{coordinationLabels[key].title}</p>
                        <p className="content-compact text-default-secondary">{coordinationLabels[key].helper}</p>
                      </div>
                    </div>
                    <Badge variant="outline" className="content-caption shrink-0">
                      {retentionText(coordinationRetention[key])}
                    </Badge>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-3 border-t border-elevation-subtle-soft pt-4">
              <div className="flex items-center justify-between">
                <p className="content-compact font-medium text-default-secondary">{t_i18n("Langfuse export")}</p>
                {isLangfuseEnvManaged ? <EnvLockBadge /> : null}
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Plug2 className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Tracing Export")}</p>
                    <p className="content-compact text-default-secondary">
                      {t_i18n("Send LLM traces to Langfuse.")}{" "}
                      <a
                        href="https://langfuse.com"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-accent hover:underline inline-flex items-center gap-1"
                      >
                        langfuse.com <ExternalLink className="h-3 w-3" />
                      </a>
                    </p>
                  </div>
                </div>
                <Badge variant={langfuseConfig?.enabled ? "success" : "outline"} className="content-caption">
                  {langfuseConfig?.enabled ? <><CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Enabled")}</> : t_i18n("Disabled")}
                </Badge>
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Key className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Credentials")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("Public key & secret key")}</p>
                  </div>
                </div>
                {langfuseConfig?.has_public_key && langfuseConfig?.has_secret_key ? (
                  <Badge variant="success" className="content-caption">
                    <CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Configured")}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="content-caption">{t_i18n("Not configured")}</Badge>
                )}
              </div>

              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-4">
                <div className="flex items-center gap-3">
                  <Globe className="h-5 w-5 text-default-secondary" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Host")}</p>
                    <p className="content-compact text-default-secondary">{langfuseConfig?.host || LANGFUSE_DEFAULT_HOST}</p>
                  </div>
                </div>
              </div>

              {langfuseConfig?.enabled && langfuseConfig?.has_public_key && langfuseConfig?.has_secret_key ? (
                <div className="space-y-2">
                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full gap-1.5"
                    onClick={() => testLangfuseConnection.mutate()}
                    disabled={testLangfuseConnection.isPending} aria-busy={testLangfuseConnection.isPending || undefined}>
                    {testLangfuseConnection.isPending && <Spinner size="sm" tone="inherit" />}{!testLangfuseConnection.isPending && <Plug2 className="h-3 w-3" />}
                    {t_i18n("Test connection")}
                  </Button>

                  {testLangfuseConnection.data ? (
                    <div
                      className={cn(
                        "flex items-center gap-2 rounded-sm p-3 content-compact",
                        testLangfuseConnection.data.status === "success"
                          ? "bg-feedback-success-primary/10 text-feedback-success-primary"
                          : "bg-feedback-error-primary/10 text-feedback-error-primary",
                      )}
                    >
                      {testLangfuseConnection.data.status === "success" ? (
                        <CheckCircle2 className="h-4 w-4 shrink-0" />
                      ) : (
                        <XCircle className="h-4 w-4 shrink-0" />
                      )}
                      <span>{testLangfuseConnection.data.message}</span>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="space-y-6">
            <div className="space-y-4">
              <div>
                <SettingsGroupTitle>{t_i18n("Local tracing")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("What the platform stores of each run, and for how long.")}
                </SettingsGroupDescription>
              </div>

              <SettingsSwitchField
                id="observability-trace-storage"
                label={t_i18n("Enable local trace storage")}
                helper={t_i18n("Off, no new trace is stored.")}
                info={t_i18n(
                  "On: every run writes its trace to the platform store, which the Traces pages read. Off: no new trace is stored, and the traces already stored stay until the retention deletes them.",
                )}
                checked={traceStorageEnabled}
                onCheckedChange={setTraceStorageEnabled}
              />

              <SettingsField
                id="observability-trace-retention-days"
                label={t_i18n("Trace retention (days)")}
                helper={t_i18n("Traces older than this are deleted automatically. Allowed range: {min} to {max}.", {
                  min: MIN_TRACE_RETENTION_DAYS,
                  max: MAX_TRACE_RETENTION_DAYS,
                })}
                info={t_i18n(
                  "How long traces are kept, in days. For example, 30 keeps one month. Empty goes back to 90 days.",
                )}
                error={
                  traceRetentionInvalid
                    ? t_i18n("Enter a number of days from {min} to {max}.", {
                        min: MIN_TRACE_RETENTION_DAYS,
                        max: MAX_TRACE_RETENTION_DAYS,
                      })
                    : undefined
                }
                className="max-w-sm"
              >
                <Input
                  ref={traceRetentionRef}
                  id="observability-trace-retention-days"
                  aria-labelledby={settingsFieldLabelId("observability-trace-retention-days")}
                  aria-describedby={settingsFieldHintId("observability-trace-retention-days")}
                  aria-invalid={traceRetentionInvalid || undefined}
                  type="number"
                  inputMode="numeric"
                  min={MIN_TRACE_RETENTION_DAYS}
                  max={MAX_TRACE_RETENTION_DAYS}
                  value={traceRetentionInput}
                  onChange={(event) => setTraceRetentionInput(event.target.value)}
                />
              </SettingsField>

              <SettingsSwitchField
                id="observability-trace-payloads"
                label={t_i18n("Store full payloads")}
                helper={t_i18n(
                  "Prompts, answers, tool arguments and tool results. Off, traces keep only timings, token counts, cost, model, tool names and status.",
                )}
                info={t_i18n(
                  "On: each trace also keeps the prompts, answers, tool arguments and tool results, which the Traces pages show to debug a run. Off: a failed run is harder to debug. The Langfuse export is not affected.",
                )}
                checked={traceStorePayloads}
                onCheckedChange={setTraceStorePayloads}
              />

              {!traceStorePayloads ? (
                <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3 content-compact text-feedback-warning-tertiary">
                  <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>{t_i18n("Disabling this limits debugging capabilities")}</span>
                </div>
              ) : null}

              {!traceStorageEnabled || !traceStorePayloads ? (
                <div
                  className="space-y-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 content-compact text-default-secondary"
                  data-testid="observability-trace-switches-note"
                >
                  <p>
                    {t_i18n(
                      "Applies within seconds to the traces written from then on. Traces already stored keep what they hold until the retention deletes them.",
                    )}
                  </p>
                  {langfuseEnabled ? (
                    <p>{t_i18n("The Langfuse export is not affected: while it is on, it still receives each call's payloads.")}</p>
                  ) : null}
                </div>
              ) : null}
            </div>

            <div className="space-y-4 border-t border-elevation-subtle-soft pt-4">
              <div>
                <SettingsGroupTitle>{t_i18n("Audit log")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("How long the record of user and administrator actions is kept.")}
                </SettingsGroupDescription>
              </div>

              <SettingsField
                id="observability-audit-log-retention-days"
                label={t_i18n("Audit log retention (days)")}
                helper={t_i18n("Audit log entries older than this are deleted automatically, every hour. Allowed range: {min} to {max}.", {
                  min: MIN_AUDIT_LOG_RETENTION_DAYS,
                  max: MAX_AUDIT_LOG_RETENTION_DAYS,
                })}
                info={t_i18n(
                  "How long audit log entries are kept, in days. For example, 365 keeps one year. Each user's last sign-in is kept past this window, so the Users page last login and the Security Audit dormant-account check keep their 30 days. Empty goes back to 90 days.",
                )}
                error={
                  auditLogRetentionInvalid
                    ? t_i18n("Enter a number of days from {min} to {max}.", {
                        min: MIN_AUDIT_LOG_RETENTION_DAYS,
                        max: MAX_AUDIT_LOG_RETENTION_DAYS,
                      })
                    : undefined
                }
                className="max-w-sm"
              >
                <Input
                  ref={auditLogRetentionRef}
                  id="observability-audit-log-retention-days"
                  aria-labelledby={settingsFieldLabelId("observability-audit-log-retention-days")}
                  aria-describedby={settingsFieldHintId("observability-audit-log-retention-days")}
                  aria-invalid={auditLogRetentionInvalid || undefined}
                  type="number"
                  inputMode="numeric"
                  min={MIN_AUDIT_LOG_RETENTION_DAYS}
                  max={MAX_AUDIT_LOG_RETENTION_DAYS}
                  value={auditLogRetentionInput}
                  onChange={(event) => setAuditLogRetentionInput(event.target.value)}
                />
              </SettingsField>
            </div>

            <div className="space-y-4 border-t border-elevation-subtle-soft pt-4">
              <div>
                <SettingsGroupTitle>{t_i18n("Coordination retention")}</SettingsGroupTitle>
                <SettingsGroupDescription>
                  {t_i18n("How long work items, shared state and worksets are kept once they are closed. 0 keeps them forever.")}
                </SettingsGroupDescription>
              </div>

              <SettingsFieldGrid>
                {COORDINATION_RETENTION_KEYS.map((key) => {
                  const id = `observability-coordination-${key.replace(/_/g, "-")}`;
                  const invalid = coordinationDays[key] === null;
                  return (
                    <SettingsField
                      key={key}
                      id={id}
                      label={coordinationLabels[key].label}
                      helper={coordinationLabels[key].helper}
                      info={t_i18n(
                        "An hourly sweep deletes what is past this window, in batches, until nothing is left. Active entries and work items still open are never deleted. 0 keeps them forever. Allowed range: {min} to {max}.",
                        { min: MIN_COORDINATION_RETENTION_DAYS, max: MAX_COORDINATION_RETENTION_DAYS },
                      )}
                      error={
                        invalid
                          ? t_i18n("Enter a number of days from {min} to {max}.", {
                              min: MIN_COORDINATION_RETENTION_DAYS,
                              max: MAX_COORDINATION_RETENTION_DAYS,
                            })
                          : undefined
                      }
                    >
                      <Input
                        ref={(element) => {
                          coordinationRefs.current[key] = element;
                        }}
                        id={id}
                        aria-labelledby={settingsFieldLabelId(id)}
                        aria-describedby={settingsFieldHintId(id)}
                        aria-invalid={invalid || undefined}
                        type="number"
                        inputMode="numeric"
                        min={MIN_COORDINATION_RETENTION_DAYS}
                        max={MAX_COORDINATION_RETENTION_DAYS}
                        value={coordinationInputs[key]}
                        onChange={(event) => setCoordinationInputs((current) => ({ ...current, [key]: event.target.value }))}
                      />
                    </SettingsField>
                  );
                })}
              </SettingsFieldGrid>
            </div>

            <div className="space-y-4 border-t border-elevation-subtle-soft pt-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <SettingsGroupTitle>{t_i18n("Langfuse export")}</SettingsGroupTitle>
                  <SettingsGroupDescription>
                    {t_i18n("Sends each model call to a Langfuse server, in addition to the local traces.")}
                  </SettingsGroupDescription>
                </div>
                {isLangfuseEnvManaged ? <EnvLockBadge /> : null}
              </div>

              {isLangfuseEnvManaged ? (
                <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 content-compact text-default-secondary">
                  {t_i18n("Langfuse is managed by environment variables and cannot be edited from the UI.")}
                </div>
              ) : (
                <>
                  <SettingsSwitchField
                    id="observability-langfuse-enabled"
                    label={
                      <span className="flex items-center gap-2">
                        {t_i18n("Enable Langfuse tracing")}
                        {langfuseConfig?.has_public_key && langfuseConfig?.has_secret_key ? (
                          <Badge variant="success" className="content-caption">
                            <CheckCircle2 className="h-3 w-3 mr-1" />
                            {t_i18n("Configured")}
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="content-caption">{t_i18n("Not configured")}</Badge>
                        )}
                      </span>
                    }
                    helper={t_i18n("Send LLM traces to Langfuse.")}
                    info={t_i18n(
                      "On: each model call is also sent, with its payloads, to the Langfuse project of the keys below. Off: nothing is sent to Langfuse. The local traces are not affected.",
                    )}
                    checked={langfuseEnabled}
                    onCheckedChange={setLangfuseEnabled}
                  />

                  <SettingsFieldGrid>
                    <SettingsField
                      id="observability-langfuse-public-key"
                      label={t_i18n("Public Key")}
                      helper={t_i18n("From the Langfuse project settings, starts with pk-lf-. Empty keeps the stored key.")}
                      info={t_i18n(
                        "The public key of the Langfuse project that receives the traces, in that project's API keys settings. It starts with pk-lf-. Empty keeps the key already stored. Only the Langfuse export reads it.",
                      )}
                    >
                      <SecretField
                        id="observability-langfuse-public-key"
                        aria-label={t_i18n("Public Key")}
                        aria-describedby={settingsFieldHintId("observability-langfuse-public-key")}
                        placeholder="pk-lf-..."
                        hasStoredValue={langfuseConfig?.has_public_key}
                        stored={langfuseConfig?.vault_secrets?.locations?.public_key?.[0] ?? null}
                        value={langfusePublicKey}
                        onChange={setLangfusePublicKey}
                      />
                    </SettingsField>

                    <SettingsField
                      id="observability-langfuse-secret-key"
                      label={t_i18n("Secret Key")}
                      helper={t_i18n("From the Langfuse project settings, starts with sk-lf-. Empty keeps the stored key.")}
                      info={t_i18n(
                        "The secret key paired with the public key, shown by Langfuse when the key pair is created. It starts with sk-lf-. Empty keeps the key already stored, which is never shown back. Only the Langfuse export reads it.",
                      )}
                    >
                      <SecretField
                        id="observability-langfuse-secret-key"
                        aria-label={t_i18n("Secret Key")}
                        aria-describedby={settingsFieldHintId("observability-langfuse-secret-key")}
                        placeholder="sk-lf-..."
                        hasStoredValue={langfuseConfig?.has_secret_key}
                        stored={langfuseConfig?.vault_secrets?.locations?.secret_key?.[0] ?? null}
                        value={langfuseSecretKey}
                        onChange={setLangfuseSecretKey}
                      />
                    </SettingsField>

                    <SettingsField
                      id="observability-langfuse-host"
                      label={t_i18n("Host")}
                      helper={t_i18n("The address of the Langfuse server. Empty uses {host}.", { host: LANGFUSE_DEFAULT_HOST })}
                      info={t_i18n(
                        "The Langfuse server the traces are sent to, with its scheme. For example, https://langfuse.example.com for a server you host.",
                      )}
                      wide
                    >
                      <Input
                        id="observability-langfuse-host"
                        aria-labelledby={settingsFieldLabelId("observability-langfuse-host")}
                        aria-describedby={settingsFieldHintId("observability-langfuse-host")}
                        value={langfuseHost}
                        onChange={(event) => setLangfuseHost(event.target.value)}
                        placeholder={LANGFUSE_DEFAULT_HOST}
                      />
                    </SettingsField>
                  </SettingsFieldGrid>
                </>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
