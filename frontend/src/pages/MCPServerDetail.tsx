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

import { useEffect, useRef } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  Terminal,
  Globe,
  Server,
  Trash2,
  TestTube2,
  Settings2,
  BarChart3,
  Activity,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { useAuth } from "@/hooks/useAuth";
import {
  useMCPServers, useDeleteMCPServer, useTestMCPServer,
} from "@/hooks/api/useMCPServerApi";
import { inferMCPLogo, TRANSPORT_DISPLAY_NAMES } from "@/lib/constants";
import { isEntityShared, canManageEntity } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { readerOAuthStatus } from "@/lib/mcp-oauth-status";
import { NewServerForm } from "./mcp-server-detail/NewServerForm";
import { OverviewTab } from "./mcp-server-detail/OverviewTab";
import { ConfigTab } from "./mcp-server-detail/ConfigTab";
import { ActivityTab } from "./mcp-server-detail/ActivityTab";

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

type Tab = "overview" | "config" | "activity";

export default function MCPServerDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  // The admin scope: an admin opens a colleague's server from Objects or an agent.
  const { data: mcpServers, isLoading } = useMCPServers({ scope: "all" });
  const deleteMCP = useDeleteMCPServer();
  const testMCP = useTestMCPServer();
  const [tab] = useTabParam<Tab>("overview");
  const [ConfirmEl, confirmSharing] = useCredentialSharingConfirm();
  const [searchParams, setSearchParams] = useSearchParams();

  // Announced once per redirect. Clearing the query param is not enough on its
  // own: React.StrictMode double-invokes effects in development, and both
  // invocations run before the cleared params are applied, so the toast was
  // raised twice. The ref makes the effect idempotent instead of relying on
  // the timing of a state update.
  const announcedRedirect = useRef(false);

  // Show toast when returning from OAuth redirect.
  //
  // The callback redirects here on failure too, rather than rendering a raw
  // error page — so a failure that is not read here is a failure the user
  // never sees: they land back on a page that looks perfectly normal, with a
  // stale "Not connected" badge and no idea why.
  useEffect(() => {
    if (announcedRedirect.current) return;
    if (searchParams.get("connected") === "true") {
      announcedRedirect.current = true;
      toast({ title: t_i18n("OAuth connected"), description: t_i18n("MCP server authorized successfully."), variant: "success" });
      setSearchParams({}, { replace: true });
      return;
    }
    const error = searchParams.get("error");
    if (error) {
      announcedRedirect.current = true;
      toast({
        title: t_i18n("OAuth failed"),
        description: error === "no_access_token"
          ? t_i18n("The authorization server returned no access token. Check the Token URL and scopes, then try again.")
          : t_i18n("The authorization could not be completed. Check the OAuth configuration, then try again."),
        variant: "error",
      });
      setSearchParams({}, { replace: true });
    }
  }, [searchParams, setSearchParams, toast, t_i18n]);

  // If id is "new", show the creation form
  if (id === "new") {
    return <NewServerForm />;
  }

  const srv = mcpServers?.find((s) => s.id === id);
  const isShared = isEntityShared(srv, user?.id);
  const canManage = canManageEntity(srv, user?.id, isAdmin);
  // On a per_user server the badge reads the reader's own account (#443).
  const oauthStatus = srv ? readerOAuthStatus(srv) : "disconnected";

  const handleDelete = async () => {
    if (!srv) return;
    // One dialog: who loses this server through what when it is deleted,
    // or the plain confirmation when nobody else uses it.
    if (await confirmSharing(
      { entity_type: "mcp_server", entity_id: srv.id, change: { delete: true } },
      { mode: "delete", fallback: { title: "Delete MCP Server", description: `Are you sure you want to delete "${srv.name}"? Agents using this server will lose access.` } },
    )) {
      deleteMCP.mutate(srv.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Server deleted"), variant: "success" });
          navigate("/mcp-servers");
        },
      });
    }
  };

  const handleTest = () => {
    if (!srv) return;
    testMCP.mutate(srv.id, {
      onSuccess: (data) => {
        // "auth_required" is a reachable server that will refuse every tool
        // call until it is authorized. Reporting it as success is what made
        // the old behaviour contradictory — a green test, then failures.
        if (data.status === "auth_required") {
          toast({
            title: t_i18n("Authorization required"),
            description: data.oauth_configured
              ? t_i18n("{count, plural, one {# tool} other {# tools}} listed, but this server refuses tool calls until you authorize it. Open Configuration and click Authorize.", { count: data.tools?.length || 0 })
              : t_i18n("{count, plural, one {# tool} other {# tools}} listed, but this server requires OAuth. Switch Authentication to OAuth 2.0 in Configuration, then authorize it.", { count: data.tools?.length || 0 }),
            variant: "info",
          });
          return;
        }
        toast({
          title: data.status === "ok" ? t_i18n("Connection successful") : t_i18n("Connection failed"),
          description: data.status === "ok" ? t_i18n("{count, plural, one {# tool discovered} other {# tools discovered}}", { count: data.tools?.length || 0 }) : (data.message || t_i18n("Failed to connect")),
          variant: data.status === "ok" ? "success" : "error",
        });
      },
    });
  };

  // Loading state
  if (isLoading) {
    return <DetailPageSkeleton backTo="/mcp-servers" backLabel="Back to MCP Servers" tabs={3} actions={1} />;
  }

  // Not found
  if (!srv) {
    return (
      <NotFoundState
        icon={Server}
        title="Server not found"
        description="This MCP server may have been deleted or you don't have access."
        actionLabel="View All Servers"
        onAction={() => navigate("/mcp-servers")}
        backTo="/mcp-servers"
        backLabel="Back to MCP Servers"
      />
    );
  }

  const logo = inferMCPLogo(srv.name);

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="space-y-6">
      {ConfirmEl}

      {/* Header actions: the test connects with the server's credentials
          and persists what it discovers, so the API wants a manage right. */}
      <EntityPageHeader
        backTo="/mcp-servers"
        backLabel="Back to MCP Servers"
        icon={
          logo ? (
            <img src={logo} alt="" className="h-7 w-7 object-contain" />
          ) : srv.transport === "stdio" ? (
            Terminal
          ) : (
            Globe
          )
        }
        title={srv.name}
        badges={
          <>
            <span className="content-base text-default-secondary">
              {t_i18n(TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport)}
            </span>
            <span className="text-default-disabled">·</span>
            <Badge variant={srv.enabled ? "success" : "outline"} className="content-caption">
              {srv.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
            </Badge>
            {srv.auth_mode === "oauth" && (
              <Badge
                variant={oauthStatus === "connected" ? "success" : oauthStatus === "error" ? "destructive" : "outline"}
                className="content-caption"
              >
                OAuth {oauthStatus === "connected" ? t_i18n("Connected") : oauthStatus === "error" ? t_i18n("Error") : t_i18n("Not connected")}
              </Badge>
            )}
            {srv.is_company_managed && <CompanyManagedBadge size="default" />}
            {isShared && <SharedBadge size="default" ownerName={srv.owner_name} />}
            {((srv.is_company_managed && !isAdmin) || (isShared && !canManage)) && (
              <ReadOnlyBadge />
            )}
          </>
        }
        actions={
          <>
            {canManage && (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={handleTest} disabled={testMCP.isPending}>
                <TestTube2 className="h-3.5 w-3.5" />
                {testMCP.isPending ? t_i18n("Testing...") : t_i18n("Test connection")}
              </Button>
            )}

            {canManage && (
              <Button aria-label={t_i18n("Delete server")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={deleteMCP.isPending}>
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
          </>
        }
      />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Tab content */}
      {tab === "overview" && <OverviewTab srv={srv} />}
      {tab === "config" && <ConfigTab srv={srv} canManage={!!canManage} />}
      {tab === "activity" && <ActivityTab srv={srv} />}
    </div>
  );
}
