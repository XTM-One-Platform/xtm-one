// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { Link, useSearchParams, useNavigate } from "react-router-dom";
import {
  Plus,
  Plug2,
  CheckCircle2,
  XCircle,
  UserRound,
} from "lucide-react";
import {
  IntegrationCatalogBrowser,
  type ProviderInstallState,
} from "@/components/integrations/integration-catalog-browser";
import { ProviderDetailsDialog } from "@/components/integrations/provider-details-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useIntegrations,
  useProviderCatalog,
} from "@/hooks/api/useIntegrationApi";
import { SharingWaysBadge } from "@/components/ui/sharing-ways-badge";
import { RelayRouteBadge } from "@/components/ui/relay-route-badge";
import { useResourceExposureSummary } from "@/hooks/api/useExposureApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { isEntityShared, formatCompactNumber } from "@/lib/utils";
import { INTEGRATION_STATUS, INTEGRATION_PROVIDER_NAMES, displayedIntegrationStatus, integrationCategoryLabel, providerInstallsStatus } from "@/lib/constants";
import { CategoryFilter } from "@/components/ui/category-filter";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { ViewToggle } from "@/components/ui/view-toggle";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { PlatformGateIcon } from "@/pages/integrations/PlatformGate";
import type { Integration, ProviderCatalogItem, ResourceExposureSummary } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

/** The query parameters the OAuth and GitHub App callbacks redirect with. */
const OAUTH_REDIRECT_PARAMS = ["connected", "github_installed", "error"] as const;

/**
 * Per-auth-type credential checklist, shared by the card footer and the list
 * row so both surfaces stay in sync as auth types are added.
 */
function CredentialIndicators({ integration, effectiveStatus }: { integration: Integration; effectiveStatus: string }) {
  const { t_i18n } = useFormatter();

  if (integration.auth_type === "oauth") {
    return (
      <>
        <div className="flex items-center gap-1 content-compact text-default-secondary">
          {integration.has_client_id ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-feedback-error-primary" />}
          {t_i18n("Client ID")}
        </div>
        <div className="flex items-center gap-1 content-compact text-default-secondary">
          {integration.has_access_token ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-default-secondary" />}
          {t_i18n("Token")}
        </div>
      </>
    );
  }

  if (integration.auth_type === "api_key" || integration.auth_type === "bot_token") {
    return (
      <div className="flex items-center gap-1 content-compact text-default-secondary">
        {integration.has_api_key ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-feedback-error-primary" />}
        {integration.auth_type === "bot_token" ? t_i18n("Bot token") : t_i18n("API key")}
      </div>
    );
  }

  if (integration.auth_type === "none") {
    return (
      <div className="flex items-center gap-1 content-compact text-default-secondary">
        {/* Credential-less providers with all-optional config (e.g.
            MITRE ATT&CK / ATLAS) are fully configured out of the box —
            a connected status counts as configured even with no
            extra_config stored. */}
        {integration.has_extra_config || effectiveStatus === "connected" ? <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" /> : <XCircle className="h-3 w-3 text-default-secondary" />}
        {t_i18n("Configured")}
      </div>
    );
  }

  return null;
}

/**
 * On an integration whose people each connect their own account, for
 * someone who acts with their own there: whether they connected it.
 */
function OwnAccountBadge({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  if (integration.credential_mode !== "per_user" || !integration.uses_own_credential) return null;
  const connected = !!integration.my_credential && integration.my_credential.status !== "reauthorize";
  return (
    <Badge variant={connected ? "success" : "warning"} className="content-compact">
      <UserRound className="h-3 w-3 mr-0.5" />
      {connected ? t_i18n("Your account: connected") : t_i18n("Connect your account")}
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// List View
// ---------------------------------------------------------------------------

/** List-view columns — name, status, category, credentials, call stats, tags. */
function useIntegrationColumns({
  userId,
  logoMap,
  categoryMap,
  exposure,
}: {
  userId?: string;
  logoMap: Record<string, string | null>;
  categoryMap: Record<string, string>;
  exposure?: Record<string, ResourceExposureSummary>;
}): EntityTableColumn<Integration>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (i) => i.name,
      cell: (i) => (
        <EntityNameCell
          icon={<ProviderLogo provider={i.provider} logo={logoMap[i.provider]} className="h-4 w-4" />}
          name={i.name}
          description={INTEGRATION_PROVIDER_NAMES[i.provider] ?? i.provider}
          // Up to seven badges per row: under the name, or they take its
          // width (the name was gone at 1024 px).
          badgesPlacement="below"
          // An array, so a row with no badge passes [] and EntityNameCell
          // draws no empty badge row. The badges
          // that decide inside themselves (own account, sharing ways, relay)
          // are listed only when they have something to show.
          badges={[
            i.is_company_managed && <CompanyManagedBadge key="company" size="default" />,
            i.is_builtin && <Badge key="builtin" variant="outline" className="content-compact border-accent/30 text-accent">{t_i18n("Built-in")}</Badge>,
            i.is_custom && <Badge key="custom" variant="outline" className="content-compact border-feedback-alert-primary/40 text-feedback-warning-primary">{t_i18n("Custom")}</Badge>,
            isEntityShared(i, userId) && <SharedBadge key="shared" size="default" ownerName={i.owner_name} />,
            i.credential_mode === "per_user" && i.uses_own_credential && <OwnAccountBadge key="own" integration={i} />,
            (exposure?.[i.id]?.ways ?? 0) > 0 && <SharingWaysBadge key="ways" size="default" summary={exposure?.[i.id]} />,
            !!i.relay_group_id && <RelayRouteBadge key="relay" size="default" groupId={i.relay_group_id} />,
          ].filter(Boolean)}
        />
      ),
    },
    {
      key: "status",
      header: "Status",
      className: "whitespace-nowrap",
      sortValue: (i) => displayedIntegrationStatus(i),
      cell: (i) => {
        const effectiveStatus = displayedIntegrationStatus(i);
        const statusCfg = INTEGRATION_STATUS[effectiveStatus] ?? INTEGRATION_STATUS.disconnected;
        const StatusIcon = statusCfg.icon;
        return (
          <span className="flex items-center gap-1.5">
            <Badge variant={statusCfg.badge} className="content-compact">
              <StatusIcon className="h-3 w-3 mr-0.5" />
              {t_i18n(statusCfg.label)}
            </Badge>
            <PlatformGateIcon integrationId={i.id} />
          </span>
        );
      },
    },
    {
      key: "category",
      header: "Category",
      // Category, Calls and Last call show from 1280 px: below it the table
      // fits its card without a horizontal scroll. hideBelow (a 56rem table),
      // so the Columns picker brings them back.
      hideBelow: "4xl",
      className: "whitespace-nowrap text-default-secondary content-compact",
      sortValue: (i) => categoryMap[i.provider],
      cell: (i) => {
        const category = categoryMap[i.provider];
        return category
          ? t_i18n(integrationCategoryLabel(category))
          : <span className="text-default-disabled">—</span>;
      },
    },
    {
      key: "credentials",
      header: "Credentials",
      className: "whitespace-nowrap",
      sortValue: (i) => i.auth_type,
      cell: (i) => (
        <div className="flex items-center gap-3">
          <CredentialIndicators
            integration={i}
            effectiveStatus={displayedIntegrationStatus(i)}
          />
        </div>
      ),
    },
    {
      key: "calls",
      header: "Calls",
      align: "right",
      hideBelow: "4xl",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (i) => i.call_count,
      cell: (i) => formatCompactNumber(i.call_count),
    },
    {
      key: "last_called",
      header: "Last call",
      align: "right",
      hideBelow: "4xl",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (i) => i.last_called_at,
      cell: (i) => i.last_called_at ? formatTimeAgo(intl, i.last_called_at) : <span className="text-default-disabled">—</span>,
    },
    {
      key: "tags",
      header: "Tags",
      cell: (i) => <TagList tags={i.tags} />,
    },
  ], [t_i18n, intl, userId, logoMap, categoryMap, exposure]);
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

const CATEGORY_PARAM = "cat";

export default function Integrations() {
  const { t_i18n, intl } = useFormatter();
  const { data: integrations, isLoading } = useIntegrations();
  const { data: catalog } = useProviderCatalog("integration", true);
  const { toast } = useToast();
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  // Only the redirect's own parameters are removed, so `?vis=` and any other
  // parameter the page writes in the same commit survive.
  const clearRedirectParams = useCallback(
    () =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const key of OAUTH_REDIRECT_PARAMS) next.delete(key);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  // Detect OAuth redirect: ?connected=provider (ref guard prevents duplicate toast in Strict Mode)
  const toastShown = useRef(false);
  useEffect(() => {
    const connected = searchParams.get("connected");
    if (connected && !toastShown.current) {
      toastShown.current = true;
      toast({ title: t_i18n("Integration connected"), description: t_i18n("{provider} OAuth flow completed successfully.", { provider: connected }), variant: "success" });
      clearRedirectParams();
    } else if (searchParams.get("github_installed") && !toastShown.current) {
      // A GitHub App installed from GitHub's own UI names no integration.
      toastShown.current = true;
      toast({
        title: t_i18n("GitHub App installed"),
        description: t_i18n("An installation made from GitHub is recorded only on integrations already connected with your GitHub account. To use it, open your GitHub integration and click Connect."),
        variant: "info",
      });
      clearRedirectParams();
    } else if (searchParams.get("error") && !toastShown.current) {
      // The GitHub App install callback redirects here when it fails.
      toastShown.current = true;
      const error = searchParams.get("error");
      toast({
        title: t_i18n("GitHub App installation failed"),
        description:
          error === "github_token_exchange_failed"
            ? t_i18n("GitHub did not accept the code of this installation. Open your GitHub integration and click Connect.")
            : error === "github_no_token"
              ? t_i18n("GitHub answered this installation without an access token. Open your GitHub integration and click Connect.")
              : error === "no_github_credentials"
                ? t_i18n("No GitHub OAuth app is configured to complete this installation. An administrator sets one in Settings > Integrations > OAuth Defaults.")
                : t_i18n("The authorization could not be completed. Check the OAuth configuration, then try again."),
        variant: "error",
      });
      clearRedirectParams();
    }
  }, [searchParams, clearRedirectParams, toast, t_i18n]);

  const integrationSearchFields = useCallback((i: Integration) => [i.name, i.provider], []);
  // No landing while a redirect parameter is still in the URL: the landing's
  // `?vis=` write would start from the URL holding it and put it back after
  // `clearRedirectParams` removed it.
  const redirectPending = OAUTH_REDIRECT_PARAMS.some((key) => searchParams.has(key));
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allIntegrations, allTags, filtered: baseFiltered, writeParams } =
    // Channel bots are a separate entity, so the list is taken whole; undefined
    // while loading, so the page lands on a segment from the loaded list.
    useEntityFilter({ items: redirectPending ? undefined : integrations, userId: user?.id, searchFields: integrationSearchFields });

  // Build provider → category lookup from catalog
  const providerCategoryMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of catalog ?? []) {
      map[item.provider] = item.integration_category;
    }
    return map;
  }, [catalog]);

  const providerLogoMap = useMemo(() => {
    const map: Record<string, string | null> = {};
    for (const item of catalog ?? []) {
      map[item.provider] = item.logo ?? null;
    }
    return map;
  }, [catalog]);

  // Unique categories from the user's integrations (based on catalog data)
  const categories = useMemo(() => {
    const cats = new Set<string>();
    for (const integ of allIntegrations) {
      const cat = providerCategoryMap[integ.provider];
      if (cat) cats.add(cat);
    }
    return [...cats].sort();
  }, [allIntegrations, providerCategoryMap]);

  // The selected category lives in the URL (`?cat=`), like the search, tags and
  // visibility segment, so a reload or a shared link shows the same list.
  // Written through the hook's writeParams so it never undoes a filter write of
  // the same tick.
  // A category no integration of the list has (a stale link) filters nothing.
  const urlCategory = searchParams.get(CATEGORY_PARAM);
  const categoryFilter = urlCategory && categories.includes(urlCategory) ? urlCategory : null;
  const setCategoryFilter = useCallback(
    (category: string | null) =>
      writeParams((prev) => {
        const next = new URLSearchParams(prev);
        if (category) next.set(CATEGORY_PARAM, category);
        else next.delete(CATEGORY_PARAM);
        return next;
      }),
    [writeParams],
  );

  // Apply category filter on top of the shared hook's results
  const filtered = categoryFilter
    ? baseFiltered.filter((i) => providerCategoryMap[i.provider] === categoryFilter)
    : baseFiltered;

  const [viewMode, setViewMode] = useViewMode("integrations");
  // Several integrations on one provider: the card cannot link to one of them,
  // so it opens the provider and lets the viewer pick.
  const [picking, setPicking] = useState<ProviderCatalogItem | null>(null);

  /** Every integration the viewer runs, keyed by provider. */
  const installsByProvider = useMemo(() => {
    const map = new Map<string, Integration[]>();
    for (const integration of allIntegrations) {
      const list = map.get(integration.provider);
      if (list) list.push(integration);
      else map.set(integration.provider, [integration]);
    }
    return map;
  }, [allIntegrations]);

  const installState = useCallback(
    (item: ProviderCatalogItem): ProviderInstallState | undefined => {
      const installs = installsByProvider.get(item.provider);
      if (!installs || installs.length === 0) return undefined;
      // Trouble wins over health (worst status first): someone scanning the
      // grid needs to see the broken Jira among the three working ones.
      return { count: installs.length, status: providerInstallsStatus(installs) };
    },
    [installsByProvider],
  );

  /**
   * What the catalog offers here: everything creatable, plus any
   * registration-only provider the viewer actually runs.
   *
   * A registration-only provider (OpenCTI, OpenAEV) cannot be created from
   * this page — it arrives through platform registration — so offering an
   * empty card for one would promise an action that 403s. Once an integration
   * exists on it, the card earns its place: it is the way to reach it.
   */
  const browsableCatalog = useMemo(
    () => (catalog ?? []).filter(
      (c) => !c.is_registration_only || installsByProvider.has(c.provider),
    ),
    [catalog, installsByProvider],
  );

  const handleProviderSelect = (item: ProviderCatalogItem) => {
    const installs = installsByProvider.get(item.provider) ?? [];
    if (installs.length === 1) {
      navigate(`/integrations/${installs[0].id}`);
      return;
    }
    if (installs.length > 1) {
      setPicking(item);
      return;
    }
    navigate(`/integrations/new?provider=${encodeURIComponent(item.provider)}`);
  };
  const addOnProvider = (item: ProviderCatalogItem) => {
    setPicking(null);
    navigate(`/integrations/new?provider=${encodeURIComponent(item.provider)}`);
  };
  const pickingInstalls = picking ? installsByProvider.get(picking.provider) ?? [] : [];
  const { data: exposure } = useResourceExposureSummary("integration");
  const columns = useIntegrationColumns({
    userId: user?.id,
    logoMap: providerLogoMap,
    categoryMap: providerCategoryMap,
    exposure: exposure?.summaries,
  });

  /** The instances of one provider, offered when a card cannot pick for you. */
  const instancePicker = (
    <ProviderDetailsDialog
      item={picking}
      open={picking !== null}
      onOpenChange={(open) => { if (!open) setPicking(null); }}
    >
      <div className="space-y-2">
        <p className="font-sans-geologica content-compact font-semibold uppercase tracking-[0.12em] text-default-secondary">
          {t_i18n("Your integrations")}
        </p>
        {pickingInstalls.map((integration) => {
          const status = displayedIntegrationStatus(integration);
          const cfg = INTEGRATION_STATUS[status] ?? INTEGRATION_STATUS.disconnected;
          const StatusIcon = cfg.icon;
          return (
            <Link
              key={integration.id}
              to={`/integrations/${integration.id}`}
              className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 transition-colors hover:border-accent/30"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate content-base font-medium">{integration.name}</p>
                <p className="content-compact text-default-secondary">
                  {integration.last_called_at ? formatTimeAgo(intl, integration.last_called_at) : t_i18n("Never used")}
                </p>
              </div>
              {isEntityShared(integration, user?.id) && <SharedBadge size="default" />}
              {integration.is_company_managed && <CompanyManagedBadge size="default" />}
              <Badge variant={cfg.badge} className="shrink-0 content-compact">
                <StatusIcon className="mr-0.5 h-3 w-3" />
                {t_i18n(cfg.label)}
              </Badge>
            </Link>
          );
        })}
        <Button
          variant="secondary"
          size="sm"
          className="w-full gap-1.5"
          onClick={() => { if (picking) addOnProvider(picking); }}
        >
          <Plus className="h-3.5 w-3.5" /> {t_i18n("Add another")}
        </Button>
      </div>
    </ProviderDetailsDialog>
  );

  // The catalog IS the grid view: one card per provider, the ones the viewer
  // already runs first. Creating an integration is a click on its provider,
  // which is why there is no "New Integration" button here.
  if (!isLoading && viewMode === "grid") {
    return (
      <>
        <IntegrationCatalogBrowser
          catalog={browsableCatalog}
          variant="browse"
          installState={installState}
          onAdd={addOnProvider}
          onSelect={handleProviderSelect}
          heroTitle={<>{t_i18n("Integrations")}</>}
          heroDescription={t_i18n("Connect services that your agents can use to act on your behalf. All credentials are encrypted in the database.")}
          heroActions={<ViewToggle value={viewMode} onChange={setViewMode} />}
          searchPlaceholder={t_i18n("Search integrations...")}
        />
        {instancePicker}
      </>
    );
  }

  return (
    <div className="space-y-6">
      {instancePicker}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Integrations")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Connect services that your agents can use to act on your behalf. All credentials are encrypted in the database.")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button className="gap-1.5" onClick={() => setViewMode("grid")}>
            <Plus className="h-4 w-4" /> {t_i18n("New Integration")}
          </Button>
        </div>
      </div>

        <>
          {!isLoading && allIntegrations.length === 0 ? (
            <EmptyState
              icon={Plug2}
              title="No integrations yet"
              description="Browse the catalog to connect a service like Gmail, Outlook, GitHub, or Slack."
              actionLabel="Browse the catalog"
              onAction={() => setViewMode("grid")}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope, tags,
            // category pill, Clear all | total, Columns, page size, view) and two-line rows.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(i) => i.id}
              rowHref={(i) => `/integrations/${i.id}`}
              defaultSortKey="name"
              storageKey="integrations"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search integrations...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              filterPills={
                categories.length > 1 && (
                  <CategoryFilter
                    collapsed
                    categories={categories}
                    selected={categoryFilter}
                    onChange={setCategoryFilter}
                    labels={Object.fromEntries(categories.map((c) => [c, t_i18n(integrationCategoryLabel(c))]))}
                  />
                )
              }
              view={{ value: viewMode, onChange: setViewMode }}
              totalAll={allIntegrations.length}
              externalFilterCount={selectedTags.length + (categoryFilter ? 1 : 0)}
              onClearAll={() => clearAll((next) => { next.delete(CATEGORY_PARAM); return next; }, { keepScope: true })}
              noResultsLabel={t_i18n("No integrations matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No integrations matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}
        </>
    </div>
  );
}
