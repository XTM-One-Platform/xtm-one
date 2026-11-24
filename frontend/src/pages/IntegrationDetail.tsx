// SPDX-License-Identifier: Apache-2.0

import { Suspense, useEffect, useRef, useState } from "react";
import { platformOrigin } from "@/lib/base-path";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  Trash2, Plug2, Settings2, ExternalLink, Download,
  Activity, BarChart3, CheckCircle2, XCircle, Info, Lock,
} from "lucide-react";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import {
  useIntegrations,
  useProviderCatalog,
  useAuthorizeIntegration,
  useDeleteIntegration,
  useExportIntegrationBundle,
  useTestIntegration,
} from "@/hooks/api/useIntegrationApi";
import { useAuth } from "@/hooks/useAuth";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { canAuthorizeIntegration, canManageEntity, cn, isEntityShared, lendsConnectedAccount } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import { INTEGRATION_STATUS, INTEGRATION_PROVIDER_NAMES, displayedIntegrationStatus } from "@/lib/constants";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { Skeleton } from "@/components/ui/skeleton";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { useToast } from "@/components/ui/toast";
import { SetupGuideDialog } from "@/components/ui/setup-guide-dialog";
import { isA2AProvider } from "@/hooks/api/useA2AApi";
import { useA2ADirection } from "@/hooks/api/useA2AClientApi";
import { A2AUnavailableNotice } from "@/components/ui/a2a-unavailable-notice";
import { resolveVariant, setupGuideFor, variantValue } from "@/lib/catalog-variants";
import { OverviewTab } from "./integration-detail/OverviewTab";
import { ConfigTab } from "./integration-detail/ConfigTab";
import { ActivityTab } from "./integration-detail/ActivityTab";
import { isProviderTabRestricted, visibleProviderTabs } from "./integration-detail/provider-tabs";
import { DeviceAuthorizationDialog } from "./integration-detail/DeviceAuthorizationDialog";
import type { DeviceAuthorization } from "@/types";

// Built-in keys plus any provider-contributed sub-page key (see provider-tabs).
type Tab = "overview" | "config" | "activity" | (string & {});

export default function IntegrationDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();

  // The admin scope: an admin opens a colleague's integration from Objects or an agent.
  const { data: integrations, isLoading } = useIntegrations({ scope: "all" });
  const { data: catalog } = useProviderCatalog("integration");
  const { data: authConfig } = useAuthConfig();
  const { user, isAdmin } = useAuth();
  const remove = useDeleteIntegration();
  const authorize = useAuthorizeIntegration();
  const exportBundle = useExportIntegrationBundle();
  const test = useTestIntegration();
  const [tab, setTab] = useTabParam<Tab>("overview");
  const [ConfirmEl, confirm] = useConfirm();
  // A sign-in with a code (OAuth2 device authorization) in progress.
  const [deviceFlow, setDeviceFlow] = useState<DeviceAuthorization | null>(null);
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();
  const [searchParams, setSearchParams] = useSearchParams();

  // The OAuth callback redirects here, where the sign-in started: with
  // `connected` once it completes, with `error` when the token
  // exchange fails - unread, the page would look normal with a stale
  // status and no reason.
  const announcedRedirect = useRef(false);
  useEffect(() => {
    const connected = searchParams.get("connected");
    if (announcedRedirect.current || (!connected && !searchParams.get("error"))) return;
    announcedRedirect.current = true;
    toast(
      connected
        ? {
          title: t_i18n("Integration connected"),
          description: t_i18n("{provider} OAuth flow completed successfully.", { provider: connected }),
          variant: "success",
        }
        : {
          title: t_i18n("OAuth failed"),
          description: t_i18n("The authorization could not be completed. Check the OAuth configuration, then try again."),
          variant: "error",
        },
    );
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        next.delete("connected");
        next.delete("error");
        next.delete("provider");
        return next;
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams, toast, t_i18n]);

  const integration = integrations?.find((i) => i.id === id);
  const catalogEntry = catalog?.find((c) => c.provider === integration?.provider);
  const baseUrl = authConfig?.base_url ?? platformOrigin();
  const a2aDirection = useA2ADirection(integration);
  const setupGuide = catalogEntry
    ? setupGuideFor(
        resolveVariant(catalogEntry, variantValue(catalogEntry, integration?.extra_config, a2aDirection)),
        integration?.auth_type,
      )
    : undefined;
  // Served by the backend from its one list of registration-only providers,
  // already scoped to XTM One mode.
  const isRegistrationOnly = !!integration?.is_registration_only;
  const hasManageRights = canManageEntity(integration, user?.id, isAdmin);
  const canManage = !isRegistrationOnly && !integration?.is_builtin && hasManageRights;
  const isShared = isEntityShared(integration, user?.id);
  const canAuthorize = !!integration && canAuthorizeIntegration(integration, user?.id, isAdmin);

  const handleOAuth = () => {
    if (!integration) return;
    authorize.mutate(integration.id, {
      onSuccess: (data) => {
        if (data.device_authorization) {
          setDeviceFlow(data.device_authorization);
        } else if (data.authorize_url) {
          window.location.href = data.authorize_url;
        }
      },
      onError: (err) => {
        toast({ title: t_i18n("OAuth failed"), description: err.message || t_i18n("Could not start authorization flow."), variant: "error" });
      },
    });
  };

  const handleDelete = async () => {
    if (!integration) return;
    if (await confirmSharing({ entity_type: "integration", entity_id: integration.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete integration?", description: "Agents using this integration will lose access. This action cannot be undone." } })) {
      remove.mutate(integration.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Integration deleted"), variant: "success" });
          navigate("/integrations");
        },
      });
    }
  };

  const handleExportBundle = async () => {
    if (!integration || !isAdmin) return;
    const confirmed = await confirm({
      title: "Export bundle with credentials?",
      description:
        "The exported ZIP embeds this integration's configuration, including decrypted API keys and secrets, so it can be re-imported elsewhere. Only share it through trusted channels.",
      confirmLabel: "Export",
      variant: "warning",
    });
    if (!confirmed) return;
    try {
      const { blob, filename } = await exportBundle.mutateAsync({
        provider: integration.provider,
        integrationId: integration.id,
      });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      // Prefer the backend's versioned filename (<slug>-<version>.zip) so
      // repeated exports don't silently overwrite each other locally.
      a.download = filename ?? `${integration.provider.replace(":", "-")}-bundle.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      toast({ title: t_i18n("Bundle exported"), variant: "success" });
    } catch (error) {
      toast({
        title: t_i18n("Export failed"),
        description: error instanceof Error ? error.message : t_i18n("Unable to export bundle."),
        variant: "error",
      });
    }
  };

  const handleTest = () => {
    if (!integration) return;
    test.mutate(integration.id, {
      onSuccess: (data) => {
        toast({
          title: data.status === "ok" ? t_i18n("Connection successful") : t_i18n("Connection failed"),
          description: data.message,
          variant: data.status === "ok" ? "success" : "error",
        });
      },
    });
  };

  // Loading / not found states
  if (isLoading) {
    return <DetailPageSkeleton backTo="/integrations" backLabel="Back to Integrations" tabs={3} actions={1} />;
  }

  if (!integration) {
    return (
      <NotFoundState
        icon={Plug2}
        title="Integration not found"
        description="This integration may have been deleted or you don't have access."
        actionLabel="View All Integrations"
        onAction={() => navigate("/integrations")}
        backTo="/integrations"
        backLabel="Back to Integrations"
      />
    );
  }

  const effectiveStatus = displayedIntegrationStatus(integration);
  const awaitingReauthorization = effectiveStatus === "reauthorize";
  const statusCfg = INTEGRATION_STATUS[effectiveStatus] ?? INTEGRATION_STATUS.disconnected;
  const StatusIcon = statusCfg.icon;

  // Provider-contributed sub-pages (e.g. Nova's "Rules") append after the
  // built-in tabs; empty for every integration that doesn't register any.
  const viewer = { userId: user?.id, isAdmin };
  const extraTabs = visibleProviderTabs(integration, viewer);
  const activeExtraTab = extraTabs.find((t) => t.key === tab);
  const restrictedTab = isProviderTabRestricted(integration, tab, viewer);
  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "activity", label: "Activity", icon: Activity },
    ...extraTabs.map((t) => ({ key: t.key, label: t.label, icon: t.icon })),
  ];

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo="/integrations"
        backLabel="Back to Integrations"
        icon={<ProviderLogo provider={integration.provider} logo={catalogEntry?.logo} className="h-7 w-7" />}
        tileClassName={effectiveStatus === "connected" ? "bg-feedback-success-primary/10" : effectiveStatus === "reauthorize" ? "bg-feedback-alert-primary/10" : "bg-elevation-hover"}
        title={integration.name}
        badges={
          <>
            <span className="content-base text-default-secondary">{catalogEntry?.name || INTEGRATION_PROVIDER_NAMES[integration.provider] || integration.provider}</span>
            <span className="text-default-disabled">·</span>
            <Badge variant={statusCfg.badge} className="content-caption">
              <StatusIcon className="h-2.5 w-2.5 mr-0.5" />
              {t_i18n(statusCfg.label)}
            </Badge>
            {integration.is_company_managed && <CompanyManagedBadge size="default" />}
            {integration.is_builtin && <Badge variant="outline" className="content-caption border-accent/30 text-accent">{t_i18n("Built-in")}</Badge>}
            {integration.is_custom && <Badge variant="outline" className="content-caption border-feedback-alert-primary/40 text-feedback-warning-primary">{t_i18n("Custom")}</Badge>}
            {isShared && <SharedBadge size="default" ownerName={integration.owner_name} />}
            {((integration.is_company_managed && !isAdmin) || (isShared && !hasManageRights)) && <ReadOnlyBadge />}
          </>
        }
        actions={
          <>
            {catalogEntry && setupGuide && (
              <SetupGuideDialog guide={setupGuide} providerName={catalogEntry.name} />
            )}
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={handleTest} disabled={test.isPending}>
              <Settings2 className="h-3.5 w-3.5" />
              {test.isPending ? t_i18n("Testing...") : t_i18n("Test connection")}
            </Button>

            {/* Not for a spec-imported provider (OpenAPI): its tools live in
                the integration's own spec, which a provider bundle cannot carry. */}
            {isAdmin && !catalogEntry?.spec_import && (
              <EntityHeaderWideOnly>
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                onClick={handleExportBundle}
                disabled={exportBundle.isPending}
              >
                <Download className="h-3.5 w-3.5" />
                {exportBundle.isPending ? t_i18n("Exporting...") : t_i18n("Export bundle")}
              </Button>
              </EntityHeaderWideOnly>
            )}

            {/* Every OAuth integration offers it (#8386), whatever its status:
                without a client ID the backend names what is missing. */}
            {canAuthorize && integration.auth_type === "oauth" && (
              <Tip content={lendsConnectedAccount(integration) ? t_i18n("This integration is shared: authorizing it lets everyone it is shared with act using your credentials.") : undefined}>
                <Button
                  variant={awaitingReauthorization ? "default" : "secondary"}
                  size="sm"
                  className={cn("gap-1.5", awaitingReauthorization && "bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill")}
                  onClick={handleOAuth}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {awaitingReauthorization || integration.has_access_token ? t_i18n("Re-authorize") : t_i18n("Authorize")}
                </Button>
              </Tip>
            )}

            {canManage && (
              <EntityHeaderWideOnly>
              <Button aria-label={t_i18n("Delete integration")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={remove.isPending}>
                <Trash2 className="h-4 w-4" />
              </Button>
              </EntityHeaderWideOnly>
            )}
            {/* Below 1280 px: Export Bundle and Delete fold into the
                "More actions" menu. */}
            <EntityHeaderMoreActions
              items={[
                ...(isAdmin && !catalogEntry?.spec_import
                  ? [{ id: "export-bundle", label: t_i18n("Export bundle"), icon: Download, onSelect: () => void handleExportBundle(), disabled: exportBundle.isPending }]
                  : []),
                ...(canManage
                  ? [{ id: "delete", label: t_i18n("Delete integration"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void handleDelete(), disabled: remove.isPending }]
                  : []),
              ]}
            />
          </>
        }
      />

      {/* Test result banner */}
      {test.data && (
        <div className={cn(
          "content-base px-4 py-3 rounded-sm flex items-center gap-2",
          test.data.status === "ok" ? "bg-feedback-success-primary/10 text-feedback-success-primary"
            : test.data.status === "info" ? "bg-feedback-info-primary/10 text-feedback-info-primary"
            : "bg-feedback-error-primary/10 text-feedback-error-primary",
        )}>
          {test.data.status === "ok" ? <CheckCircle2 className="h-4 w-4" /> : test.data.status === "info" ? <Info className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {test.data.message}
        </div>
      )}

      {/* While A2A is not offered (it follows agentic chat), every A2A card on
          the tabs renders itself away; this is the one line that says why.
          Renders nothing otherwise. */}
      {isA2AProvider(integration.provider) && (
        <A2AUnavailableNotice
          hint={t_i18n("This partner registration is kept, but nothing is served to or sent to the partner until agentic chat is available again.")}
        />
      )}

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Tab content */}
      {tab === "overview" && (
        <OverviewTab
          integration={integration}
          catalogEntry={catalogEntry}
          onAuthorize={canAuthorize ? handleOAuth : undefined}
          // Each person connects their own account to a per_user integration:
          // its Authorize signs them in for themselves.
          onConnectOwnAccount={integration.uses_own_credential ? handleOAuth : undefined}
          connectingOwnAccount={authorize.isPending}
          setTab={setTab}
        />
      )}
      {tab === "config" && (
        <ConfigTab
          integration={integration}
          catalogEntry={catalogEntry}
          canManage={canManage}
          isRegistrationOnly={isRegistrationOnly}
          baseUrl={baseUrl}
        />
      )}
      {tab === "activity" && <ActivityTab integration={integration} />}
      {activeExtraTab && (
        <Suspense fallback={<Skeleton className="h-64 w-full" />}>
          {activeExtraTab.render(integration)}
        </Suspense>
      )}
      {restrictedTab && (
        <EmptyState
          icon={Lock}
          title="This page is not available to you"
          description="Your role does not give access to this page of the integration. Ask an administrator if you need it."
          actionLabel="Open the Overview"
          actionIcon={BarChart3}
          onAction={() => setTab("overview")}
        />
      )}
      {ConfirmEl}
      <DeviceAuthorizationDialog integrationId={integration.id} flow={deviceFlow} onClose={() => setDeviceFlow(null)} />
      {ConsentEl}
    </div>
  );
}
