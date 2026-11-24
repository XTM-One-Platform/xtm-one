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

/**
 * Shared components for catalog pages.
 *
 * All components follow the platform design system: `rounded-sm` borders,
 * standard `Input` focus styles, `Badge` for tags, glass-morphism cards.
 */

import {
  ArrowLeft, ArrowRight, Calendar, Check, CheckCircle2, ChevronLeft,
  Cog, Copy, Download, Plus, Search, User, AlertCircle, Building2, Link2,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { VariablesNeedingValueList } from "@/components/ui/missing-variables-banner";
import {
  downloadCatalogItem,
  useAddCatalogItemToAccount,
  type CatalogInstallConfig,
} from "@/hooks/api/usePublicCatalogApi";
import { useIntegrations, useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { cn, formatShortDate } from "@/lib/utils";
import { COPY_FEEDBACK_DELAY, ICON_MAP, INTEGRATION_PROVIDER_NAMES, providerLogoSrc } from "@/lib/constants";
import type {
  AddToAccountResponse,
  CatalogEntityType,
  CatalogIntegrationRef,
  CatalogResourceRef,
} from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Search bar
// ---------------------------------------------------------------------------

export function CatalogSearch({
  value,
  onChange,
  placeholder = "Search...",
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className={cn("relative", className)}>
      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-default-secondary" />
      <Input aria-label={t_i18n(placeholder)}
        placeholder={t_i18n(placeholder)}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="pl-9"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tag badges
// ---------------------------------------------------------------------------

function TagList({ tags }: { tags: string[] }) {
  if (!tags.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {tags.map((t) => (
        <Badge key={t} variant="cyan" className="text-[11px]">{t}</Badge>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grid card
// ---------------------------------------------------------------------------

export function CatalogCard({
  to,
  icon,
  name,
  description,
  tags,
  meta,
  sharedBy,
}: {
  to: string;
  icon: ReactNode;
  name: string;
  description: string | null;
  tags: string[];
  meta?: ReactNode;
  sharedBy?: string | null;
}) {
  return (
    <Link
      to={to}
      className={cn(
        "group relative flex flex-col overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm",
        "transition-all duration-300",
        "hover:border-accent/30 hover:layer-1 bg-elevation-default hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]",
      )}
    >
      <div className="pointer-events-none absolute inset-0 rounded-sm bg-gradient-to-br from-accent/[0.03] to-transparent opacity-0 transition-opacity duration-300 group-hover:opacity-100" />

      <div className="relative flex flex-col flex-1">
        {/* Header: icon + title + description — fixed layout */}
        <div className="flex items-start gap-3">
          <div className="flex items-center justify-center rounded-sm bg-accent/10 p-2 shrink-0 transition-colors duration-300 group-hover:bg-accent/15">
            {icon}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-sans-geologica content-base font-semibold truncate">{name}</h3>
              <ArrowRight className="h-3 w-3 shrink-0 text-default-disabled -translate-x-1 opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:opacity-100 group-hover:text-accent" />
            </div>
            <p className="mt-0.5 content-compact text-default-secondary/80 line-clamp-2 leading-relaxed min-h-[2.5rem]">
              {description || "\u00A0"}
            </p>
          </div>
        </div>

        {/* Spacer pushes footer to bottom */}
        <div className="flex-1" />

        {/* Footer: tags + shared by + meta — always rendered */}
        <div className="flex items-center justify-between gap-2 pt-2 mt-3 border-t border-elevation-subtle-soft min-h-[28px]">
          <div className="flex items-center gap-2 min-w-0 overflow-hidden">
            {tags.length > 0 ? (
              <TagList tags={tags.slice(0, 3)} />
            ) : (
              <span className="content-caption text-default-disabled invisible">no tags</span>
            )}
            {sharedBy && (
              <span className="flex items-center gap-1 content-caption text-default-secondary shrink-0">
                <User className="h-2.5 w-2.5" />
                {sharedBy}
              </span>
            )}
          </div>
          {meta && <div className="content-compact text-default-secondary shrink-0">{meta}</div>}
        </div>
      </div>
    </Link>
  );
}

/** The catalog's card grid: the loaded cards and their loading cards share it (#11345). */
const CATALOG_GRID = "grid gap-4 sm:grid-cols-2 lg:grid-cols-3";

/**
 * A catalog card with its content greyed out, in CatalogCard's own box: the same border,
 * p-5 padding, 32 px icon tile, title line, two-line description block and footer
 * under its hairline, so the cards land where their placeholders stood.
 */
function CatalogCardSkeleton({ index }: { index: number }) {
  return (
    <div data-testid="catalog-card-skeleton" className="flex flex-col overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5">
      <div data-slot="card-head" className="flex items-start gap-3">
        <Skeleton className="size-8 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex h-5 items-center">
            <Skeleton className={cn("h-3.5", index % 2 === 0 ? "w-3/4" : "w-3/5")} />
          </div>
          <div data-slot="card-description" className="mt-0.5 min-h-[2.5rem] space-y-1.5 pt-1">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
      </div>
      <div data-slot="card-footer" className="flex items-center justify-between gap-2 pt-2 mt-3 border-t border-elevation-subtle-soft min-h-[28px]">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-16" />
      </div>
    </div>
  );
}

/**
 * A catalog list page while its first page is read: the loaded page header (icon tile,
 * title, count line) and search box, then a page of card placeholders on the loaded grid.
 */
export function CatalogListSkeleton({ count }: { count: number }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6" role="status" aria-busy="true" aria-label={t_i18n("Loading...")} data-testid="catalog-list-skeleton">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div data-slot="header" className="flex items-center gap-3">
          <Skeleton className="size-10" />
          <div className="space-y-2">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-3 w-28" />
          </div>
        </div>
        <Skeleton className="h-9 w-full sm:max-w-xs" />
      </div>
      <div className={CATALOG_GRID}>
        {Array.from({ length: count }).map((_, index) => (
          <CatalogCardSkeleton key={index} index={index} />
        ))}
      </div>
    </div>
  );
}

/**
 * A catalog detail page while its item is read: DetailShell's own layout with its content
 * greyed out (the real back link, the download and add actions, the header card with its
 * 48 px tile, title, description, dates and tags), then one section block.
 */
export function CatalogDetailSkeleton({ backTo, backLabel }: { backTo: string; backLabel: string }) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6" role="status" aria-busy="true" aria-label={t_i18n("Loading...")} data-testid="catalog-detail-skeleton">
      <div className="flex items-center justify-between gap-4">
        <Button variant="ghost" size="sm" onClick={() => navigate(backTo)} className="gap-1.5">
          <ArrowLeft className="h-4 w-4" />
          {t_i18n(backLabel)}
        </Button>
        <div data-slot="actions" className="flex items-center gap-2">
          <Skeleton className="h-8 w-32" />
          <Skeleton className="h-8 w-36" />
        </div>
      </div>
      <div data-slot="header" className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-6 space-y-4">
        <div className="flex items-start gap-4">
          <Skeleton className="size-12 shrink-0" />
          <div className="flex-1 space-y-2.5 pt-1">
            <Skeleton className="h-6 w-64" />
            <Skeleton className="h-3.5 w-3/4" />
          </div>
        </div>
        <div data-slot="meta" className="flex items-center gap-4">
          <Skeleton className="h-3 w-28" />
          <Skeleton className="h-3 w-28" />
        </div>
        <div className="flex gap-1.5">
          <Skeleton className="h-4 w-14" />
          <Skeleton className="h-4 w-16" />
        </div>
      </div>
      <div data-slot="section" className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 space-y-3">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-4/5" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resource binding badge (company-managed = green, needs binding = amber)
// ---------------------------------------------------------------------------

export function ResourceBadge({
  name,
  isCompanyManaged,
  description,
}: {
  name: string;
  isCompanyManaged: boolean;
  description?: string | null;
}) {
  const { t_i18n } = useFormatter();
  if (isCompanyManaged) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-sm bg-feedback-success-primary/10 border border-feedback-success-primary/20 px-2.5 py-1 text-[11px] font-medium text-feedback-success-primary">
        <CheckCircle2 className="h-3 w-3" />
        {name}
        <span className="text-[9px] text-feedback-success-primary/60">({t_i18n("auto")})</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 px-2.5 py-1 content-caption font-medium text-feedback-warning-tertiary">
      <Link2 className="h-3 w-3" />
      {description || name}
      <span className="text-[9px] text-feedback-warning-primary">({t_i18n("needs binding")})</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Included badge (for resources cloned directly into the user's account)
// ---------------------------------------------------------------------------

export function IncludedBadge({ name, setupNote }: { name: string; setupNote?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex items-center gap-1.5 rounded-sm bg-accent/10 border border-accent/20 px-2.5 py-1 text-[11px] font-medium text-accent">
      <CheckCircle2 className="h-3 w-3" />
      {name}
      {setupNote
        ? <span className="text-[9px] text-feedback-warning-primary">({t_i18n(setupNote)})</span>
        : <span className="text-[9px] text-accent/60">({t_i18n("included")})</span>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Integration binding badge (with provider logo)
// ---------------------------------------------------------------------------

export function IntegrationBadge({
  provider,
  name,
  isCompanyManaged,
}: {
  provider: string;
  name?: string;
  isCompanyManaged?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const displayName = name || INTEGRATION_PROVIDER_NAMES[provider] || provider;
  const logoSrc = providerLogoSrc(provider);
  // Keyed by its src: a badge reused for another provider (the lists key by
  // index) gets a fresh <img>, never the one a failed logo hid.
  const logo = (
    <img key={logoSrc} src={logoSrc} alt="" className="h-3.5 w-3.5" onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
  );
  if (isCompanyManaged) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-sm bg-feedback-success-primary/10 border border-feedback-success-primary/20 px-2.5 py-1 text-[11px] font-medium text-feedback-success-primary">
        {logo}
        {displayName}
        <span className="text-[9px] text-feedback-success-primary/60">({t_i18n("auto")})</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 px-2.5 py-1 content-caption font-medium text-feedback-warning-tertiary">
      {logo}
      {INTEGRATION_PROVIDER_NAMES[provider] || provider}
      <span className="text-[9px] text-feedback-warning-primary">({t_i18n("needs binding")})</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Setup wizard dialog for agents
// ---------------------------------------------------------------------------

export function CatalogSetupWizard({
  open,
  onClose,
  entityType,
  entityId,
  entityName,
  entityIcon,
  integrationProviders,
  knowledgeBases,
  mcpServers,
  customTools,
  skills,
  prompts,
  assignmentCount,
}: {
  open: boolean;
  onClose: () => void;
  entityType: CatalogEntityType;
  entityId: string;
  entityName: string;
  entityIcon?: string;
  integrationProviders?: CatalogIntegrationRef[];
  knowledgeBases?: CatalogResourceRef[];
  mcpServers?: CatalogResourceRef[];
  customTools?: CatalogResourceRef[];
  skills?: CatalogResourceRef[];
  prompts?: CatalogResourceRef[];
  assignmentCount?: number;
}) {
  const navigate = useNavigate();
  const addMutation = useAddCatalogItemToAccount();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { data: userIntegrations } = useIntegrations();
  const { data: catalog } = useProviderCatalog("integration");

  const [name, setName] = useState(entityName);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  // The install result, kept when it left variables without a value:
  // the wizard then ends on a screen that names them.
  const [installed, setInstalled] = useState<AddToAccountResponse | null>(null);

  if (!open) return null;

  const openInstalled = (res: AddToAccountResponse) => {
    const routeMap: Record<string, string> = {
      agents: "/agents",
      prompts: "/prompts",
      skills: "/skills",
      "mcp-servers": "/mcp-servers",
      "custom-tools": "/tools",
    };
    setInstalled(null);
    onClose();
    navigate(`${routeMap[entityType] || "/"}/${res.id}`);
  };

  if (installed) {
    const needing = installed.variables_needing_value ?? [];
    const leftOut = installed.warnings ?? [];
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim backdrop-blur-sm animate-fade-in">
        <div className="relative w-full max-w-lg rounded-sm border border-elevation-subtle bg-elevation-default p-6 shadow-overlay space-y-4">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/30 to-transparent" />
          <div className="flex items-center gap-3">
            <div className="rounded-sm bg-accent/10 p-2.5">
              <CheckCircle2 className="h-5 w-5 text-accent" />
            </div>
            <div>
              <h2 className="font-sans-geologica text-lg font-bold">{t_i18n("Installed successfully")}</h2>
              <p className="content-compact text-default-secondary">
                {t_i18n("{name} is now available in your account.", { name: installed.name })}
              </p>
            </div>
          </div>
          {leftOut.length > 0 && (
            <div className="rounded-sm bg-feedback-alert-primary/5 p-3 space-y-2" data-testid="catalog-install-left-out">
              <p className="content-compact text-feedback-warning-primary leading-relaxed">
                {t_i18n("Some parts of the agent were not copied, or were copied only in part:")}
              </p>
              <ul className="list-disc space-y-1 pl-4 content-compact text-default-secondary">
                {leftOut.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          )}
          {needing.length > 0 && (
            <div className="rounded-sm bg-feedback-alert-primary/5 p-3 space-y-2">
              <p className="content-compact text-feedback-warning-primary leading-relaxed">
                {t_i18n(
                  "{count, plural, one {One variable still needs a value: a secret is never copied with an agent, so set it before the agent can use it.} other {# variables still need a value: a secret is never copied with an agent, so set them before the agent can use them.}}",
                  { count: needing.length },
                )}
              </p>
              <VariablesNeedingValueList variables={needing} />
            </div>
          )}
          <div className="flex justify-end gap-3 pt-2 border-t border-elevation-subtle-soft">
            {needing.length > 0 && (
              <Button
                variant="ghost"
                onClick={() => {
                  setInstalled(null);
                  onClose();
                  navigate("/connections/variables");
                }}
              >
                {t_i18n("Set values in Variables")}
              </Button>
            )}
            <Button onClick={() => openInstalled(installed)}>
              {t_i18n("Open agent")}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const IconComp = ICON_MAP[entityIcon || ""] ?? ICON_MAP.Bot;

  const needsBindingProviders = (integrationProviders || []).filter(
    (ip) => !ip.is_company_managed,
  );
  const autoBindProviders = (integrationProviders || []).filter(
    (ip) => ip.is_company_managed,
  );

  const connectedIntegrations = userIntegrations?.filter(
    (i) => i.status === "connected",
  ) || [];

  const getConnectedForProvider = (providerKey: string) => {
    const providerKeys = catalog
      ?.filter((c) => c.provider === providerKey)
      .map((c) => c.provider) || [providerKey];
    return connectedIntegrations.filter((i) =>
      providerKeys.includes(i.provider),
    );
  };

  const autoKBs = (knowledgeBases || []).filter((kb) => kb.is_company_managed);
  const newKBs = (knowledgeBases || []).filter((kb) => !kb.is_company_managed);
  const allMCP = mcpServers || [];
  const autoTools = (customTools || []).filter((t) => t.is_company_managed);
  const needsTools = (customTools || []).filter((t) => !t.is_company_managed);
  const allSkills = skills || [];
  const allPrompts = prompts || [];

  const hasResources =
    (integrationProviders?.length ?? 0) > 0 ||
    (knowledgeBases?.length ?? 0) > 0 ||
    allMCP.length > 0 ||
    (customTools?.length ?? 0) > 0 ||
    allSkills.length > 0 ||
    allPrompts.length > 0;

  const handleInstall = () => {
    const integrationBindings: Record<string, string[]> = {};
    for (const [provKey, integId] of Object.entries(bindings)) {
      if (integId) integrationBindings[provKey] = [...(integrationBindings[provKey] || []), integId];
    }

    const config: CatalogInstallConfig = {
      name: name.trim() || undefined,
      integration_bindings:
        Object.keys(integrationBindings).length > 0
          ? integrationBindings
          : undefined,
    };

    addMutation.mutate(
      { type: entityType, id: entityId, config },
      {
        onSuccess: (res) => {
          if (res.variables_needing_value?.length || res.warnings?.length) {
            setInstalled(res);
            return;
          }
          toast({
            title: t_i18n("Installed successfully"),
            description: t_i18n("{name} is now available in your account.", { name: res.name }),
            variant: "success",
          });
          openInstalled(res);
        },
        onError: (err) => {
          toast({
            title: t_i18n("Installation failed"),
            description: err.message,
            variant: "error",
          });
        },
      },
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim backdrop-blur-sm animate-fade-in">
      <div className="relative w-full max-w-2xl max-h-[85vh] overflow-y-auto rounded-sm border border-elevation-subtle bg-elevation-default p-6 shadow-overlay space-y-5">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/30 to-transparent" />

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="rounded-sm bg-accent/10 p-2.5">
              <IconComp className="h-5 w-5 text-accent" />
            </div>
            <div>
              <h2 className="font-sans-geologica text-lg font-bold">
                {entityType === "agents" ? t_i18n("Install Agent") : t_i18n("Install Resource")}
              </h2>
              <p className="content-compact text-default-secondary">
                {t_i18n("Configure before adding to your account")}
              </p>
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <ChevronLeft className="h-4 w-4 mr-1" />
            {t_i18n("Cancel")}
          </Button>
        </div>

        {/* Name */}
        <div className="space-y-1.5">
          <label className="content-compact font-medium text-default-secondary">{t_i18n("Name")}</label>
          <Input aria-label={t_i18n("Name")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t_i18n("Enter a name...")}
          />
        </div>

        {/* Auto-bound resources summary */}
        {hasResources && (
          <div className="space-y-3">
            {/* Auto-bound integrations */}
            {autoBindProviders.length > 0 && (
              <ResourceSection icon={<Building2 className="h-3.5 w-3.5" />} label="Auto-bound Integrations" variant="success">
                <p className="text-[11px] text-feedback-success-primary/70 mb-2">
                  {t_i18n("These company-managed integrations will be automatically connected")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {autoBindProviders.map((ip, i) => (
                    <IntegrationBadge key={i} provider={ip.provider} name={ip.name} isCompanyManaged />
                  ))}
                </div>
              </ResourceSection>
            )}

            {/* Integrations needing binding */}
            {needsBindingProviders.length > 0 && (
              <ResourceSection icon={<Cog className="h-3.5 w-3.5" />} label="Integrations Requiring Setup" variant="warning">
                <p className="text-[11px] text-default-secondary mb-2">
                  {t_i18n("Select which connected integration to use for each. You can skip and configure later.")}
                </p>
                {needsBindingProviders.map((ip) => {
                  const connected = getConnectedForProvider(ip.provider);
                  return (
                    <div key={ip.provider} className="rounded-sm bg-elevation-hover p-3 space-y-2">
                      <div className="flex items-center gap-2">
                        <img src={providerLogoSrc(ip.provider)} alt="" className="h-4 w-4" onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
                        <span className="content-compact font-medium">
                          {INTEGRATION_PROVIDER_NAMES[ip.provider] || ip.provider}
                        </span>
                      </div>
                      {connected.length > 0 ? (
                        <div className="flex flex-wrap gap-2">
                          {connected.map((integ) => (
                            <button
                              key={integ.id}
                              className={cn(
                                "flex items-center gap-2 rounded-sm border px-3 py-2 content-compact font-medium transition-colors",
                                bindings[ip.provider] === integ.id
                                  ? "border-accent/40 bg-accent/10 text-accent"
                                  : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle",
                              )}
                              onClick={() =>
                                setBindings({
                                  ...bindings,
                                  [ip.provider]:
                                    bindings[ip.provider] === integ.id
                                      ? ""
                                      : integ.id,
                                })
                              }
                            >
                              <img src={providerLogoSrc(integ.provider)} alt="" className="h-4 w-4" />
                              {integ.name}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 content-compact text-feedback-warning-primary">
                          <AlertCircle className="h-3.5 w-3.5" />
                          <span>
                            {t_i18n("No connected integration.")}{" "}
                            <button
                              className="text-accent hover:underline"
                              onClick={() => navigate("/integrations/new")}
                            >
                              {t_i18n("Connect one")}
                            </button>
                          </span>
                        </div>
                      )}
                    </div>
                  );
                })}
              </ResourceSection>
            )}

            {/* Knowledge Bases */}
            {(autoKBs.length > 0 || newKBs.length > 0) && (
              <ResourceSection
                icon={<Building2 className="h-3.5 w-3.5" />}
                label="Knowledge Bases"
                variant={newKBs.length > 0 ? "mixed" : "success"}
              >
                {newKBs.length > 0 && (
                  <p className="text-[11px] text-default-secondary mb-2">
                    {t_i18n("Non-shared knowledge bases will be recreated empty. You will need to add documents after installation.")}
                  </p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  {autoKBs.map((kb) => (
                    <ResourceBadge key={kb.id} name={kb.name} isCompanyManaged description={kb.description} />
                  ))}
                  {newKBs.map((kb) => (
                    <IncludedBadge key={kb.id} name={kb.name} setupNote="needs documents" />
                  ))}
                </div>
              </ResourceSection>
            )}

            {/* MCP Servers (definition included, secrets need configuring) */}
            {allMCP.length > 0 && (
              <ResourceSection
                icon={<Building2 className="h-3.5 w-3.5" />}
                label="MCP Servers"
                variant="mixed"
              >
                <p className="text-[11px] text-default-secondary mb-2">
                  {t_i18n("Server definitions will be copied. Environment variables and secrets need to be configured after installation.")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {allMCP.map((s) => (
                    <IncludedBadge key={s.id} name={s.name} setupNote="env vars needed" />
                  ))}
                </div>
              </ResourceSection>
            )}

            {/* Custom Tools */}
            {(autoTools.length > 0 || needsTools.length > 0) && (
              <ResourceSection
                icon={<Building2 className="h-3.5 w-3.5" />}
                label="Custom Tools"
                variant={needsTools.length > 0 ? "mixed" : "success"}
              >
                {needsTools.length > 0 && (
                  <p className="text-[11px] text-default-secondary mb-2">
                    {t_i18n("Custom tools that are not shared company-wide are not copied.")}
                  </p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  {autoTools.map((t) => (
                    <ResourceBadge key={t.id} name={t.name} isCompanyManaged />
                  ))}
                  {needsTools.map((t) => (
                    <ResourceBadge key={t.id} name={t.name} isCompanyManaged={false} />
                  ))}
                </div>
              </ResourceSection>
            )}

            {/* Skills (always included directly) */}
            {allSkills.length > 0 && (
              <ResourceSection
                icon={<Building2 className="h-3.5 w-3.5" />}
                label="Skills"
                variant="success"
              >
                <p className="text-[11px] text-accent/70 mb-2">
                  {t_i18n("Skills will be copied into your account automatically")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {allSkills.map((s) => (
                    <IncludedBadge key={s.id} name={s.name} />
                  ))}
                </div>
              </ResourceSection>
            )}

            {/* Prompts (always included directly) */}
            {allPrompts.length > 0 && (
              <ResourceSection
                icon={<Building2 className="h-3.5 w-3.5" />}
                label="Prompts"
                variant="success"
              >
                <p className="text-[11px] text-accent/70 mb-2">
                  {t_i18n("Prompts will be copied into your account automatically")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {allPrompts.map((p) => (
                    <IncludedBadge key={p.id} name={p.name} />
                  ))}
                </div>
              </ResourceSection>
            )}
          </div>
        )}

        {/* Assignment count */}
        {(assignmentCount ?? 0) > 0 && (
          <div className="rounded-sm bg-accent/5 border border-accent/10 p-3">
            <p className="content-compact text-accent font-medium">
              {t_i18n("Includes {count, plural, one {# pre-configured assignment} other {# pre-configured assignments}} (disabled by default)", { count: assignmentCount! })}
            </p>
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-3 pt-2 border-t border-elevation-subtle-soft">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={handleInstall}
            disabled={!name.trim() || addMutation.isPending}
          >
            <Plus className="h-4 w-4 mr-1.5" />
            {addMutation.isPending ? t_i18n("Installing...") : t_i18n("Install")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function ResourceSection({
  icon,
  label,
  variant,
  children,
}: {
  icon: ReactNode;
  label: string;
  variant: "success" | "warning" | "mixed";
  children: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const borderColor =
    variant === "success"
      ? "border-feedback-success-primary/20"
      : variant === "warning"
        ? "border-feedback-alert-primary/20"
        : "border-elevation-subtle-soft";
  const bgColor =
    variant === "success"
      ? "bg-feedback-success-primary/[0.03]"
      : variant === "warning"
        ? "bg-feedback-alert-primary/[0.03]"
        : "bg-elevation-hover";

  return (
    <div className={cn("rounded-sm border p-3 space-y-2", borderColor, bgColor)}>
      <div className="flex items-center gap-2">
        <span className="text-default-secondary">{icon}</span>
        <span className="content-compact font-medium">{t_i18n(label)}</span>
      </div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail page shell (updated to use setup wizard)
// ---------------------------------------------------------------------------

export function DetailShell({
  backTo,
  backLabel,
  entityType,
  entityId,
  icon,
  name,
  description,
  tags,
  createdAt,
  updatedAt,
  sharedBy,
  children,
  extraActions,
  onInstall,
  downloadOmittedTools,
}: {
  backTo: string;
  backLabel: string;
  entityType: CatalogEntityType;
  entityId: string;
  icon: ReactNode;
  name: string;
  description: string | null;
  tags: string[];
  createdAt: string;
  updatedAt?: string;
  sharedBy?: string | null;
  children: ReactNode;
  extraActions?: ReactNode;
  onInstall?: () => void;
  /** Custom tools the download leaves out because their publisher kept the code. */
  downloadOmittedTools?: string[];
}) {
  const navigate = useNavigate();
  const addMutation = useAddCatalogItemToAccount();
  const { toast } = useToast();
  const { t_i18n, intl } = useFormatter();

  const handleQuickAdd = () => {
    if (onInstall) {
      onInstall();
      return;
    }
    addMutation.mutate(
      { type: entityType, id: entityId },
      {
        onSuccess: (res) => {
          toast({
            title: t_i18n("Added to your account"),
            description: t_i18n("{name} is now available in your {entityType}.", { name: res.name, entityType }),
          });
        },
      },
    );
  };

  const handleDownload = () => {
    downloadCatalogItem(entityType, entityId).catch(() => {
      toast({ title: t_i18n("Download failed"), variant: "error" });
    });
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between gap-4">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate(backTo)}
          className="gap-1.5"
        >
          <ArrowLeft className="h-4 w-4" />
          {t_i18n(backLabel)}
        </Button>
        <div className="flex items-center gap-2">
          {extraActions}
          <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={handleDownload}>
            <Download className="h-3.5 w-3.5" />
            {t_i18n("Download JSON")}
          </Button>
          <Button
            size="sm"
            onClick={handleQuickAdd}
            disabled={addMutation.isPending}
            className="gap-1.5 h-8"
          >
            <Plus className="h-3.5 w-3.5" />
            {addMutation.isPending ? t_i18n("Adding...") : t_i18n("Add to my account")}
          </Button>
        </div>
      </div>

      {(downloadOmittedTools?.length ?? 0) > 0 && (
        <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
          <AlertCircle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
          <p className="content-compact text-feedback-warning-primary leading-relaxed">
            {t_i18n("The downloaded file leaves out the code of these custom tools, which the publisher keeps to themselves: {tools}", { tools: downloadOmittedTools!.join(", ") })}
          </p>
        </div>
      )}

      <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-6 backdrop-blur-sm space-y-4">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/30 to-transparent" />

        <div className="flex items-start gap-4">
          <div className="rounded-sm bg-accent/10 p-3">{icon}</div>
          <div className="flex-1 space-y-1.5">
            <h1 className="font-sans-geologica text-2xl font-bold tracking-tight">{name}</h1>
            {description && (
              <p className="content-base text-default-secondary leading-relaxed">{description}</p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-4 content-compact text-default-secondary">
          <span className="flex items-center gap-1.5">
            <Calendar className="h-3 w-3" />
            {t_i18n("Created {date}", { date: formatShortDate(createdAt) })}
          </span>
          {updatedAt && (
            <span className="flex items-center gap-1.5">
              <Calendar className="h-3 w-3" />
              {t_i18n("Updated {timeAgo}", { timeAgo: formatTimeAgo(intl, updatedAt) })}
            </span>
          )}
          {sharedBy && (
            <span className="flex items-center gap-1.5">
              <User className="h-3 w-3" />
              {t_i18n("Shared by {name}", { name: sharedBy })}
            </span>
          )}
        </div>

        <TagList tags={tags} />
      </div>

      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Copy button
// ---------------------------------------------------------------------------

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  return (
    <Button variant="outline" size="sm" onClick={handleCopy} className="gap-1.5">
      {copied ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? t_i18n("Copied!") : t_i18n(label)}
    </Button>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

export function CatalogEmpty({ message = "No items found" }: { message?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-24 text-center animate-fade-in">
      <div className="rounded-sm bg-accent/10 p-5">
        <Search className="h-7 w-7 text-accent" />
      </div>
      <div className="space-y-1">
        <p className="content-base font-medium text-default-secondary">{t_i18n(message)}</p>
        <p className="content-compact text-default-secondary">{t_i18n("Try adjusting your search terms")}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page header
// ---------------------------------------------------------------------------

export function CatalogPageHeader({
  icon,
  title,
  count,
}: {
  icon: ReactNode;
  title: string;
  count?: number;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center justify-center rounded-sm bg-accent/10 p-2.5">{icon}</div>
      <div className="space-y-0.5">
        <h1 className="font-sans-geologica text-2xl font-bold tracking-tight">{t_i18n(title)}</h1>
        {count !== undefined && (
          <p className="content-compact text-default-secondary">
            <span className="font-medium text-accent">{count}</span>{" "}
            {/* Noun selected via ICU plural on count so plural rules stay
                locale-correct; the number is rendered separately for styling. */}
            {t_i18n("{count, plural, one {item in catalog} other {items in catalog}}", { count })}
          </p>
        )}
      </div>
    </div>
  );
}
