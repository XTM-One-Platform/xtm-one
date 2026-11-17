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

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Icon, Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { EmptyState } from "@/components/ui/empty-state";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import {
  useCreateCustomToolSource,
  useCustomToolSources,
  useDeleteCustomToolSource,
  useSyncCustomToolSource,
  useUpdateCustomToolSource,
} from "@/hooks/api/useCustomToolSourceApi";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import type {
  CustomToolSource,
  CustomToolSourceInterval,
  CustomToolSourceProvider,
  CustomToolSyncEntry,
  CustomToolSyncReport,
} from "@/types";

const INTERVALS: CustomToolSourceInterval[] = ["manual", "6h", "12h", "daily", "weekly"];
const NO_INTEGRATION = "__none__";

const MANIFEST_EXAMPLE = `tools/whois-lookup/
  tool.yaml        # the manifest below
  main.py          # the code (or main.js / index.js, or "entry")
  test_main.py     # optional test code (or main.test.js)

# tool.yaml
name: Whois lookup
description: Looks a domain up in WHOIS
timeout: 60
tags: [osint]
input_schema:
  type: object
  properties:
    domain: { type: string, description: The domain }
  required: [domain]
tests:
  - name: dry run
    inputs: { domain: example.com }
    dry_run: true
    expect: { contains: [example.com] }`;

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

function useIntervalLabel() {
  const { t_i18n } = useFormatter();
  return (interval: CustomToolSourceInterval) =>
    ({
      manual: t_i18n("Manual only"),
      "6h": t_i18n("Every 6 hours"),
      "12h": t_i18n("Every 12 hours"),
      daily: t_i18n("Daily"),
      weekly: t_i18n("Weekly"),
    })[interval];
}

function SyncStatusBadge({ source }: { source: CustomToolSource }) {
  const { t_i18n } = useFormatter();
  if (!source.last_sync_status) return <Badge variant="outline" className="content-caption">{t_i18n("Never synced")}</Badge>;
  if (source.last_sync_status === "ok") return <Badge variant="success" className="content-caption">{t_i18n("Synced")}</Badge>;
  if (source.last_sync_status === "partial") return <Badge variant="warning" className="content-caption">{t_i18n("Partly synced")}</Badge>;
  return <Badge variant="destructive" className="content-caption">{t_i18n("Sync failed")}</Badge>;
}

// ---------------------------------------------------------------------------
// Sync report
// ---------------------------------------------------------------------------

function ReportSection({ title, entries, tone }: { title: string; entries: CustomToolSyncEntry[]; tone?: "error" }) {
  const { t_i18n } = useFormatter();
  if (entries.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className={tone === "error" ? "content-compact font-medium text-feedback-error-primary" : "content-compact font-medium"}>
        {title} ({entries.length})
      </div>
      <ul className="space-y-1">
        {entries.map((e) => {
          const tests = typeof e.tests === "object" ? e.tests : null;
          return (
            <li key={e.path} className="content-compact text-default-secondary">
              {e.tool_id ? (
                <Link to={`/tools/${e.tool_id}`} className="font-medium text-default-primary hover:underline">{e.name ?? e.path}</Link>
              ) : (
                <span className="font-medium text-default-primary">{e.name ?? e.path}</span>
              )}
              {e.name && (
                <>
                  {" "}
                  <code className="font-mono-plex">{e.path || "/"}</code>
                </>
              )}
              {e.error && <p className="text-feedback-error-primary whitespace-pre-wrap">{e.error}</p>}
              {tests && (
                <ul className="ml-4 list-disc">
                  {tests.failures.map((f) => (
                    <li key={`${f.kind}-${f.name}`} className="whitespace-pre-wrap break-words">
                      {f.name}{f.message ? `: ${f.message}` : ""}
                    </li>
                  ))}
                  {tests.failures.length === 0 && <li>{t_i18n("Tests did not pass")}</li>}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function SyncReportView({ report, source }: { report: CustomToolSyncReport; source: CustomToolSource }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateCustomToolSource();
  return (
    <div className="space-y-3 rounded-sm bg-elevation-hover p-3">
      <p className="content-compact text-default-secondary">
        {t_i18n("{created} created · {updated} updated · {unchanged} unchanged · {rejected} rejected by their tests · {disabled} disabled", {
          created: report.created.length,
          updated: report.updated.length,
          unchanged: report.unchanged.length,
          rejected: report.rejected.length,
          disabled: report.disabled.length,
        })}
      </p>
      <ReportSection title={t_i18n("Rejected: the active version is kept")} entries={report.rejected} tone="error" />
      <ReportSection title={t_i18n("Could not be read")} entries={report.errors} tone="error" />
      <ReportSection title={t_i18n("Created")} entries={report.created} />
      <ReportSection title={t_i18n("Updated")} entries={report.updated} />
      <ReportSection title={t_i18n("Disabled: gone from the repository")} entries={report.disabled} />
      <ReportSection title={t_i18n("Enabled again: back in the repository")} entries={report.restored ?? []} />
      {source.detached_paths.length > 0 && (
        <div className="space-y-1">
          <div className="content-compact font-medium">{t_i18n("Detached ({count})", { count: source.detached_paths.length })}</div>
          <ul className="space-y-1">
            {source.detached_paths.map((path) => (
              <li key={path} className="flex items-center gap-2 content-compact text-default-secondary">
                <code className="font-mono-plex">{path}</code>
                {source.can_manage && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 content-caption"
                    disabled={update.isPending}
                    onClick={() =>
                      update.mutate({ id: source.id, detached_paths: source.detached_paths.filter((p) => p !== path) })
                    }
                  >
                    {t_i18n("Sync it again")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Source form
// ---------------------------------------------------------------------------

interface SourceFormValues {
  name: string;
  provider: CustomToolSourceProvider;
  repository: string;
  ref: string;
  path: string;
  integration_id: string;
  sync_interval: CustomToolSourceInterval;
  is_company_managed: boolean;
}

function SourceDialog({
  open,
  onOpenChange,
  source,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  source?: CustomToolSource;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const intervalLabel = useIntervalLabel();
  const { data: integrations } = useIntegrations();
  const create = useCreateCustomToolSource();
  const update = useUpdateCustomToolSource();
  const sync = useSyncCustomToolSource();
  const [values, setValues] = useState<SourceFormValues>(() => ({
    name: source?.name ?? "",
    provider: source?.provider ?? "github",
    repository: source?.repository ?? "",
    ref: source?.ref ?? "",
    path: source?.path ?? "",
    integration_id: source?.integration_id ?? NO_INTEGRATION,
    sync_interval: source?.sync_interval ?? "daily",
    is_company_managed: source?.is_company_managed ?? false,
  }));
  const set = <K extends keyof SourceFormValues>(key: K, value: SourceFormValues[K]) =>
    setValues((v) => ({ ...v, [key]: value }));
  const candidates = useMemo(
    () => (integrations ?? []).filter((i) => i.provider === values.provider && i.status === "connected"),
    [integrations, values.provider],
  );
  const pending = create.isPending || update.isPending || sync.isPending;
  const canSave = values.name.trim() && values.repository.trim() && !pending;

  const submit = () => {
    const payload = {
      name: values.name.trim(),
      repository: values.repository.trim(),
      ref: values.ref.trim(),
      path: values.path.trim(),
      integration_id: values.integration_id === NO_INTEGRATION ? "" : values.integration_id,
      sync_interval: values.sync_interval,
    };
    if (source) {
      update.mutate(
        { id: source.id, ...payload },
        {
          onSuccess: () => {
            toast({ title: t_i18n("Repository updated"), variant: "success" });
            onOpenChange(false);
          },
          onError: (err) => toast({ title: t_i18n("Could not save the repository"), description: err.message, variant: "error" }),
        },
      );
      return;
    }
    create.mutate(
      {
        ...payload,
        provider: values.provider,
        integration_id: payload.integration_id || null,
        is_company_managed: values.is_company_managed,
      },
      {
        onSuccess: (created) => {
          onOpenChange(false);
          // A first sync right away: the tools appear without another click.
          sync.mutate(created.id, {
            onSuccess: ({ report, source: synced }) =>
              toast({
                title: report ? t_i18n("Repository synced") : t_i18n("The repository could not be read"),
                description: report ? undefined : synced.last_sync_error ?? undefined,
                variant: report ? "success" : "error",
              }),
          });
        },
        onError: (err) => toast({ title: t_i18n("Could not save the repository"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-base">{source ? t_i18n("Edit repository") : t_i18n("Add a repository")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Each directory of the repository holding a tool.yaml becomes a custom tool. A new or changed tool is activated only when its tests pass.")}
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto space-y-3 pr-1">
          <div className="space-y-1">
            <Label htmlFor="source-name">{t_i18n("Name")}</Label>
            <Input id="source-name" aria-label={t_i18n("Name")} value={values.name} onChange={(e) => set("name", e.target.value)} className="h-8" />
          </div>
          <div className="grid gap-3 sm:grid-cols-[140px_1fr]">
            <div className="space-y-1">
              <Label>{t_i18n("Host")}</Label>
              <Select
                value={values.provider}
                disabled={!!source}
                onValueChange={(v) => setValues((cur) => ({ ...cur, provider: v as CustomToolSourceProvider, integration_id: NO_INTEGRATION }))}
              >
                <SelectTrigger aria-label={t_i18n("Host")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="github">GitHub</SelectItem>
                  <SelectItem value="gitlab">GitLab</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="source-repository">{t_i18n("Repository")}</Label>
              <Input
                id="source-repository"
                aria-label={t_i18n("Repository")}
                value={values.repository}
                placeholder={values.provider === "github" ? "owner/repository" : "group/project"}
                onChange={(e) => set("repository", e.target.value)}
                className="h-8 font-mono-plex"
              />
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="source-ref">{t_i18n("Branch, tag or commit")}</Label>
              <Input
                id="source-ref"
                aria-label={t_i18n("Branch, tag or commit")}
                value={values.ref}
                placeholder={t_i18n("Default branch")}
                onChange={(e) => set("ref", e.target.value)}
                className="h-8 font-mono-plex"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="source-path">{t_i18n("Sub-directory")}</Label>
              <Input
                id="source-path"
                aria-label={t_i18n("Sub-directory")}
                value={values.path}
                placeholder={t_i18n("Whole repository")}
                onChange={(e) => set("path", e.target.value)}
                className="h-8 font-mono-plex"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label>{t_i18n("Credential")}</Label>
            <SearchableSelect
              aria-label={t_i18n("Credential")}
              searchable="auto"
              pinned={[{ value: NO_INTEGRATION, label: t_i18n("None: a public repository") }]}
              options={candidates.map((i) => ({ value: i.id, label: i.name }))}
              value={values.integration_id}
              onValueChange={(v) => set("integration_id", v)}
            />
            <p className="content-caption text-default-secondary">
              {values.provider === "github"
                ? t_i18n("A private repository is read with a connected GitHub integration.")
                : t_i18n("A private project, or a self-managed GitLab, is read with a connected GitLab integration.")}
            </p>
          </div>
          <div className="space-y-1">
            <Label>{t_i18n("Automatic sync")}</Label>
            <Select value={values.sync_interval} onValueChange={(v) => set("sync_interval", v as CustomToolSourceInterval)}>
              <SelectTrigger aria-label={t_i18n("Automatic sync")}><SelectValue /></SelectTrigger>
              <SelectContent>
                {INTERVALS.map((i) => (
                  <SelectItem key={i} value={i}>{intervalLabel(i)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {isAdmin && !source && (
            <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover p-3">
              <div>
                <Label htmlFor="source-company">{t_i18n("Company-managed")}</Label>
                <p className="content-caption text-default-secondary">
                  {t_i18n("Its tools are company-managed: visible to everyone, edited by admins only. Cannot be changed later.")}
                </p>
              </div>
              <Switch id="source-company" checked={values.is_company_managed} onCheckedChange={(v) => set("is_company_managed", v)} />
            </div>
          )}
          <details className="rounded-sm border border-elevation-subtle-soft p-3">
            <DisclosureSummary className="content-compact">{t_i18n("Repository layout")}</DisclosureSummary>
            <pre className="mt-2 content-caption font-mono-plex text-default-secondary whitespace-pre overflow-x-auto">{MANIFEST_EXAMPLE}</pre>
          </details>
        </div>
        <div className="flex justify-end gap-2 pt-3 border-t border-elevation-subtle-soft">
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>{t_i18n("Cancel")}</Button>
          <Button size="sm" onClick={submit} disabled={!canSave} aria-busy={pending || undefined}>
            {pending && <Spinner size="sm" tone="inherit" />}
            {source ? t_i18n("Save") : t_i18n("Add and sync")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Source card
// ---------------------------------------------------------------------------

function SourceCard({ source }: { source: CustomToolSource }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const intervalLabel = useIntervalLabel();
  const sync = useSyncCustomToolSource();
  const remove = useDeleteCustomToolSource();
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTools, setDeleteTools] = useState(false);
  const [showReport, setShowReport] = useState(false);
  const report = source.last_sync_report;

  const syncNow = () =>
    sync.mutate(source.id, {
      onSuccess: ({ report: r }) => {
        setShowReport(true);
        toast({
          title: r ? t_i18n("Repository synced") : t_i18n("The repository could not be read"),
          variant: r ? (r.status === "ok" ? "success" : "info") : "error",
        });
      },
      onError: (err) => toast({ title: t_i18n("Sync failed"), description: err.message, variant: "error" }),
    });

  return (
    <Card data-testid="tool-source-card">
      <CardContent className="p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <p className="content-base font-medium">{source.name}</p>
              <SyncStatusBadge source={source} />
              {source.is_company_managed && <Badge variant="outline" className="content-caption">{t_i18n("Company-managed")}</Badge>}
              {!source.enabled && <Badge variant="outline" className="content-caption">{t_i18n("Paused")}</Badge>}
            </div>
            <a href={source.repository_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 content-compact text-accent hover:underline">
              {source.provider === "github" ? "GitHub" : "GitLab"} · {source.repository}
              <Icon name="external-link" size={16} />
            </a>
            <p className="content-caption text-default-secondary">
              {t_i18n("Ref: {ref} · Directory: {path} · {interval} · {count, plural, one {# tool} other {# tools}}", {
                ref: source.ref || t_i18n("default branch"),
                path: source.path || "/",
                interval: intervalLabel(source.sync_interval),
                count: source.tool_count,
              })}
              {source.integration_name && ` · ${t_i18n("via {integration}", { integration: source.integration_name })}`}
            </p>
            {source.last_synced_at && (
              <p className="content-caption text-default-secondary">
                {t_i18n("Last sync {when}", { when: formatTimeAgo(intl, source.last_synced_at) })}
                {source.last_commit_sha && (
                  <>
                    {" · "}
                    <code className="font-mono-plex">{source.last_commit_sha.slice(0, 12)}</code>
                  </>
                )}
              </p>
            )}
            {source.last_sync_status === "error" && source.last_sync_error && (
              <p className="content-compact text-feedback-error-primary whitespace-pre-wrap">{source.last_sync_error}</p>
            )}
          </div>
          {source.can_manage && (
            <div className="flex items-center gap-2">
              <Button size="sm" className="gap-1.5" onClick={syncNow} disabled={sync.isPending} aria-busy={sync.isPending || undefined}>
                {sync.isPending && <Spinner size="sm" tone="inherit" />}
                {!sync.isPending && <Icon name="refresh-cw" size={16} />}
                {sync.isPending ? t_i18n("Syncing...") : t_i18n("Sync now")}
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setEditOpen(true)}>{t_i18n("Edit")}</Button>
              <Button
                variant="ghost"
                size="sm"
                aria-label={t_i18n("Delete repository")}
                className="h-8 w-8 p-0 text-feedback-error-primary"
                onClick={() => { setDeleteTools(false); setDeleteOpen(true); }}
              >
                <Icon name="trash-2" size={16} />
              </Button>
            </div>
          )}
        </div>
        {(report || source.detached_paths.length > 0) && (
          <div className="space-y-2">
            <button
              type="button"
              className="flex items-center gap-1 content-compact text-default-secondary hover:text-default-primary"
              aria-expanded={showReport}
              onClick={() => setShowReport(!showReport)}
            >
              <Icon name={showReport ? "chevron-down" : "chevron-right"} size={16} />
              {t_i18n("Last sync report")}
            </button>
            {showReport && report && <SyncReportView report={report} source={source} />}
          </div>
        )}
      </CardContent>
      {editOpen && <SourceDialog open={editOpen} onOpenChange={setEditOpen} source={source} />}
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={t_i18n("Delete this repository?")}
        description={
          deleteTools
            ? t_i18n("The source and its {count, plural, one {# tool} other {# tools}} are deleted; agents lose them.", { count: source.tool_count })
            : t_i18n("The source is deleted; its {count, plural, one {# tool} other {# tools}} stay, detached and editable.", { count: source.tool_count })
        }
        confirmLabel={t_i18n("Delete")}
        variant="danger"
        onConfirm={() =>
          remove.mutate(
            { id: source.id, deleteTools },
            { onError: (err) => toast({ title: t_i18n("Could not delete the repository"), description: err.message, variant: "error" }) },
          )
        }
      >
        {source.tool_count > 0 && (
          <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover p-3">
            <Label htmlFor={`delete-tools-${source.id}`}>{t_i18n("Also delete its tools")}</Label>
            <Switch id={`delete-tools-${source.id}`} checked={deleteTools} onCheckedChange={setDeleteTools} />
          </div>
        )}
      </ConfirmDialog>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tab
// ---------------------------------------------------------------------------

/** The Tools page's Repositories tab: GitHub / GitLab repositories synced into custom tools. */
export function ToolSourcesTab() {
  const { t_i18n } = useFormatter();
  const { data: sources, isLoading } = useCustomToolSources();
  const [addOpen, setAddOpen] = useState(false);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="content-compact text-default-secondary max-w-3xl">
          {t_i18n("Sync custom tools from a GitHub or GitLab repository. Synced tools are read-only here: they change in the repository, and a new version is activated only when its declared tests pass.")}
        </p>
        <Button className="gap-1.5" onClick={() => setAddOpen(true)}>
          <Icon name="plus" size={16} /> {t_i18n("Add a repository")}
        </Button>
      </div>
      {isLoading ? (
        <div className="flex justify-center py-8"><Spinner /></div>
      ) : !sources?.length ? (
        <EmptyState
          icon={() => <Icon name="folder-open" size={24} />}
          title="No repository yet"
          description="Add a GitHub or GitLab repository to sync its tools."
          actionLabel="Add a repository"
          onAction={() => setAddOpen(true)}
        />
      ) : (
        <div className="space-y-3">
          {sources.map((s) => <SourceCard key={s.id} source={s} />)}
        </div>
      )}
      {addOpen && <SourceDialog open={addOpen} onOpenChange={setAddOpen} />}
    </div>
  );
}
