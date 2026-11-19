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

import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Plus, Server, Terminal, Globe, Download,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { cn, isEntityShared, formatCompactNumber } from "@/lib/utils";
import { inferMCPLogo, TRANSPORT_DISPLAY_NAMES } from "@/lib/constants";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { useAuth } from "@/hooks/useAuth";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { readerOAuthStatus } from "@/lib/mcp-oauth-status";
import { PlatformMcpUserSection } from "@/components/ui/platform-mcp-user-section";
import { MCPImportDialog } from "@/components/ui/mcp-import-dialog";
import type { MCPServer } from "@/types";
import { ResourceSharingWaysBadge } from "@/components/ui/sharing-ways-badge";
import { RelayRouteBadge } from "@/components/ui/relay-route-badge";

function ServerCard({ srv, userId }: { srv: MCPServer; userId?: string }) {
  const { t_i18n } = useFormatter();
  const logo = inferMCPLogo(srv.name);
  const isShared = isEntityShared(srv, userId);

  return (
    <Link to={`/mcp-servers/${srv.id}`} className="block h-full">
      <Card
        className={cn(
          "transition-all cursor-pointer hover:bg-elevation-hover h-full",
          srv.enabled ? "border-elevation-subtle" : "border-elevation-subtle-soft opacity-60",
        )}
      >
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              {logo ? (
                <img src={logo} alt="" className="h-5 w-5 object-contain" />
              ) : srv.transport === "stdio" ? (
                <Terminal className="h-5 w-5 text-accent" />
              ) : (
                <Globe className="h-5 w-5 text-accent" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium truncate">{srv.name}</p>
              <p className="content-caption text-default-secondary">{srv.transport === "stdio" ? t_i18n("Subprocess (stdio)") : srv.transport === "streamable_http" ? "Streamable HTTP" : "HTTP (SSE)"}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <Badge variant={srv.enabled ? "success" : "outline"} className="content-caption">
                {srv.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
              </Badge>
              {srv.is_company_managed && <CompanyManagedBadge />}
              {isShared && <SharedBadge ownerName={srv.owner_name} />}
              <ResourceSharingWaysBadge kind="mcp_server" id={srv.id} />
              <RelayRouteBadge groupId={srv.relay_group_id} />
            </div>
          </div>

          {/* Metadata — pushed to bottom */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            <Badge variant="outline" className="content-caption">{srv.transport}</Badge>
            {srv.command && <span className="content-caption text-default-secondary font-mono-plex truncate">{srv.command}</span>}
            {srv.url && <span className="content-caption text-default-secondary font-mono-plex truncate">{srv.url}</span>}
            {srv.has_env && <Badge variant="outline" className="content-caption">env</Badge>}
            {srv.auth_mode === "oauth" && (
              <Badge variant={readerOAuthStatus(srv) === "connected" ? "success" : "outline"} className="content-caption">
                {readerOAuthStatus(srv) === "connected" ? "oauth" : t_i18n("oauth (pending)")}
              </Badge>
            )}
            <TagList tags={srv.tags} className="ml-auto" />
            {(!srv.tags || srv.tags.length === 0) && srv.args && srv.args.length > 0 && (
              <span className="content-caption text-default-secondary ml-auto">{t_i18n("{count, plural, one {# arg} other {# args}}", { count: srv.args.length })}</span>
            )}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

/** List-view columns — name, status, transport, endpoint, auth, tools, tags. */
function useServerColumns(userId?: string): EntityTableColumn<MCPServer>[] {
  const { t_i18n } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (s) => s.name,
      cell: (s) => {
        const logo = inferMCPLogo(s.name);
        return (
          <EntityNameCell
            icon={logo
              ? <img src={logo} alt="" className="h-4 w-4 object-contain" />
              : s.transport === "stdio"
                ? <Terminal className="h-4 w-4 text-accent" />
                : <Globe className="h-4 w-4 text-accent" />}
            name={s.name}
            muted={!s.enabled}
            badges={
              <>
                {s.is_company_managed && <CompanyManagedBadge />}
                {isEntityShared(s, userId) && <SharedBadge ownerName={s.owner_name} />}
                <ResourceSharingWaysBadge kind="mcp_server" id={s.id} />
                <RelayRouteBadge groupId={s.relay_group_id} />
              </>
            }
          />
        );
      },
    },
    {
      key: "status",
      header: "Status",
      sortValue: (s) => s.enabled,
      cell: (s) => (
        <Badge variant={s.enabled ? "success" : "outline"} className="content-caption">
          {s.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
        </Badge>
      ),
    },
    {
      key: "transport",
      header: "Transport",
      className: "whitespace-nowrap",
      sortValue: (s) => s.transport,
      cell: (s) => (
        <Badge variant="outline" className="content-caption">
          {TRANSPORT_DISPLAY_NAMES[s.transport] ?? s.transport}
        </Badge>
      ),
    },
    {
      key: "endpoint",
      header: "Endpoint",
      sortValue: (s) => s.command || s.url,
      cell: (s) => (
        <span className="block max-w-[260px] truncate font-mono-plex content-caption text-default-secondary">
          {s.command || s.url || "—"}
        </span>
      ),
    },
    {
      key: "auth",
      header: "Auth",
      className: "whitespace-nowrap",
      sortValue: (s) => s.auth_mode === "oauth" ? `oauth:${readerOAuthStatus(s)}` : "headers",
      cell: (s) => s.auth_mode === "oauth" ? (
        <Badge variant={readerOAuthStatus(s) === "connected" ? "success" : "outline"} className="content-caption">
          {readerOAuthStatus(s) === "connected" ? "oauth" : t_i18n("oauth (pending)")}
        </Badge>
      ) : s.has_env || s.has_headers ? (
        <Badge variant="outline" className="content-caption">{s.has_env ? "env" : "headers"}</Badge>
      ) : (
        <span className="text-default-disabled">—</span>
      ),
    },
    {
      key: "tools",
      header: "Tools",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      // null = tools never discovered, which is unknown rather than zero — left
      // as null so EntityTable's empty-last rule keeps it out of the count run.
      sortValue: (s) => s.tool_count,
      cell: (s) => s.tool_count ?? <span className="text-default-disabled">—</span>,
    },
    {
      key: "calls",
      header: "Calls",
      align: "right",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (s) => s.call_count,
      cell: (s) => formatCompactNumber(s.call_count),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (s) => <TagList tags={s.tags} />,
    },
  ], [t_i18n, userId]);
}

export default function MCPServers() {
  const navigate = useNavigate();
  const [importOpen, setImportOpen] = useState(false);
  const { t_i18n } = useFormatter();
  const { data: mcpServers, isLoading } = useMCPServers();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const mcpSearchFields = useCallback((s: MCPServer) => [s.command, s.url], []);
  const [viewMode, setViewMode] = useViewMode("mcp-servers");
  const columns = useServerColumns(user?.id);
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allServers, allTags, filtered } =
    useEntityFilter({ items: mcpServers, userId: user?.id, searchFields: mcpSearchFields });

  return (
    <div className="space-y-10">
      {/* Two peer sections, not one page with a widget: above, the endpoints
          the platform *exposes* to external clients; below, the servers the
          user configures for agents to call *out* to. Same protocol, opposite
          direction — merging them into one list is what made the distinction
          invisible. */}
      <PlatformMcpUserSection />

      <MCPImportDialog open={importOpen} onOpenChange={setImportOpen} />

      <section className="space-y-6">
        {/* The action sits beside the title only when there is room for it:
            on a narrow viewport it crowded the heading, which wrapped under
            it. Stacking below is the honest fallback. */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-3xl font-bold">{t_i18n("Connected MCP Servers")}</h2>
            <p className="mt-2 text-default-secondary">
              {t_i18n("Connect MCP servers so your agents can use external tools (GitHub, databases, search, ...).")}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {/* Secondary to "Add Server": importing is the faster path for
                anyone who already has a config file, but adding one by hand
                is still the action most people come here for.

                Unlike "Add Server" it stays visible while the list is empty,
                because the empty state does not offer it — and a first-time
                user holding their existing client config is exactly who has
                the most to gain from it. */}
            <AgenticGateDialog feature="Importing MCP servers">
              <Button variant="outline" className="gap-1.5" onClick={() => setImportOpen(true)}>
                <Download className="h-4 w-4" /> {t_i18n("Import")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
            {/* Hidden while the empty state is showing, because that state
                already carries the same call to action. It comes back as soon
                as there is a list, which is when the empty state is gone and
                this is the only way left to add one. */}
            {allServers.length > 0 && (
              <AgenticGateDialog feature="Adding MCP servers">
                <Button className="gap-1.5" onClick={() => navigate("/mcp-servers/new")}>
                  <Plus className="h-4 w-4" /> {t_i18n("Add Server")}
                  <AgenticEEBadgeForButton />
                </Button>
              </AgenticGateDialog>
            )}
          </div>
        </div>

        <>
          {!isLoading && allServers.length === 0 ? (
            <EmptyState
              icon={Server}
              title="No MCP servers yet"
              requiresAi
              description="Add your first MCP server to give your agents access to external tools."
              actionLabel={agenticAIEnabled ? "Add Server" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/mcp-servers/new") : undefined}
              /* The only way to add one while the list is empty, so it wears
                 the primary colour the header button gave up. */
              actionVariant="default"
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope, tags,
            // Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(s) => s.id}
              rowHref={(s) => `/mcp-servers/${s.id}`}
              rowClassName={(s) => s.enabled ? undefined : "opacity-60"}
              defaultSortKey="name"
              storageKey="mcp-servers"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search servers...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              view={{ value: viewMode, onChange: setViewMode }}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((srv) => <ServerCard key={srv.id} srv={srv} userId={user?.id} />)}
                      </div>
                    )
                  : undefined
              }
              totalAll={allServers.length}
              externalFilterCount={selectedTags.length}
              onClearAll={() => clearAll(undefined, { keepScope: true })}
              noResultsLabel={t_i18n("No servers matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No servers matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}
        </>
      </section>
    </div>
  );
}
