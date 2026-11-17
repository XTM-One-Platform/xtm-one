// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { Check, Copy, Terminal, Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { usePlatformMCPServers } from "@/hooks/api/usePlatformApi";
import type { PlatformMCPServer } from "@/types";
import { useIsXtmOne, usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";

/**
 * Shared list of the platform MCP servers (OpenCTI / OpenAEV / OpenCRQ),
 * used by Profile > MCP Endpoint and Settings > Integrations > MCP Endpoint.
 *
 * Data comes from `GET /platform/mcp-servers` (single source of truth for
 * URLs, tool counts, and live connection status).
 *
 * Mode-aware visibility:
 * - XTM One mode: all servers are listed — unconnected ones appear dimmed
 *   with a hint, so the capability is discoverable before registration.
 * - Copilot (non-XTM) mode: only connected servers are listed — platform
 *   registration is not the primary flow there, so unconnected placeholder
 *   rows would only add noise.
 *
 * Use `usePlatformMCPServerList()` alongside this component to decide
 * whether the surrounding card should render at all.
 */

export function usePlatformMCPServerList() {
  const { data, isLoading } = usePlatformMCPServers();
  const isXtmOne = useIsXtmOne();

  const visibleServers = useMemo(() => {
    const servers = data?.servers ?? [];
    // Connected servers first, preserving catalog order within each group.
    const connected = servers.filter((s) => s.connected);
    const notConnected = servers.filter((s) => !s.connected);
    return isXtmOne ? [...connected, ...notConnected] : connected;
  }, [data?.servers, isXtmOne]);

  return {
    isLoading,
    /** Global admin toggle (PlatformSettings.platform_mcp_enabled). */
    enabled: data?.enabled ?? true,
    /** XTM One's own MCP server (/mcp/xtm), with its own independent toggle.
     *  Undefined while the descriptor query is still loading. */
    xtmServer: data?.xtm_server,
    /** Servers to display for the current platform mode. */
    servers: visibleServers,
    connectedCount: (data?.servers ?? []).filter((s) => s.connected).length,
    /** XTM One mode — call sites use it to keep the surrounding card
     *  visible while the descriptor query is still loading. */
    isXtmOne,
  };
}

function connectionSnippet(server: PlatformMCPServer): string {
  return JSON.stringify(
    {
      mcpServers: {
        [server.provider]: {
          url: server.url,
          headers: {
            Authorization: "Bearer <your-api-key>",
          },
        },
      },
    },
    null,
    2,
  );
}

interface PlatformMCPServerListProps {
  servers: PlatformMCPServer[];
  /** Show the per-server "Config" button that copies a client snippet. */
  showConfig?: boolean;
  /** Dim every row (Settings renders this when the admin toggle is off). */
  disabled?: boolean;
  /** Render skeleton rows while the descriptor query is still loading,
   *  instead of an empty gap in the surrounding card. */
  isLoading?: boolean;
}

export function PlatformMCPServerList({
  servers,
  showConfig = false,
  disabled = false,
  isLoading = false,
}: PlatformMCPServerListProps) {
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null);
  const platformName = usePlatformName();
  const { t_i18n } = useFormatter();

  const handleCopyUrl = (server: PlatformMCPServer) => {
    navigator.clipboard.writeText(server.url);
    setCopiedUrl(server.provider);
    setTimeout(() => setCopiedUrl(null), COPY_FEEDBACK_DELAY);
  };

  const handleCopySnippet = (server: PlatformMCPServer) => {
    navigator.clipboard.writeText(connectionSnippet(server));
    setCopiedSnippet(server.provider);
    setTimeout(() => setCopiedSnippet(null), COPY_FEEDBACK_DELAY);
  };

  if (servers.length === 0) {
    if (isLoading) {
      return (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="rounded-sm bg-elevation-default p-4 space-y-3">
              <div className="flex items-center gap-3">
                <Skeleton className="h-6 w-6 rounded-sm" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-40" />
                  <Skeleton className="h-3 w-3/4" />
                </div>
              </div>
            </div>
          ))}
        </div>
      );
    }
    // Copilot mode with no connected platform (e.g. Settings keeps the card
    // visible while the toggle is off so the Switch stays reachable).
    return (
      <p className="content-compact text-default-secondary rounded-sm bg-elevation-hover px-3 py-2">
        {t_i18n(
          "No platform is connected yet. MCP servers activate automatically once an OpenCTI, OpenAEV, or OpenCRQ platform is registered with {platform}.",
          { platform: platformName || "XTM One" },
        )}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {servers.map((server) => (
        <div
          key={server.provider}
          className={cn(
            "rounded-sm bg-elevation-default p-4 space-y-3 transition-colors",
            server.connected && !disabled && "hover:bg-elevation-hover",
            (!server.connected || disabled) && "opacity-60",
          )}
        >
          <div className="flex items-center gap-3">
            <ProviderLogo provider={server.provider} className="h-6 w-6 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <p className="content-base font-medium">{server.name}</p>
                {server.connected ? (
                  <Tip
                    content={
                      server.healthy
                        ? t_i18n("{platform} is registered and sending heartbeats.", {
                            platform: server.platform_title ?? server.name,
                          })
                        : t_i18n("Registered, but the last heartbeat is stale. The platform may be unreachable.")
                    }
                  >
                    <Badge
                      variant={server.healthy ? "success" : "outline"}
                      className="content-caption"
                    >
                      {server.healthy ? (
                        <>
                          <Check className="h-3 w-3 mr-1" /> {t_i18n("Connected")}
                        </>
                      ) : (
                        t_i18n("Registered")
                      )}
                    </Badge>
                  </Tip>
                ) : (
                  <Badge variant="outline" className="content-caption">
                    {t_i18n("Not connected")}
                  </Badge>
                )}
                <Tip content={t_i18n("Business and knowledge tools exposed by this server. No platform administration.")}>
                  <Badge variant="secondary" className="content-caption gap-1">
                    <Wrench className="h-2.5 w-2.5" />
                    {t_i18n("{count, plural, one {# tool} other {# tools}}", { count: server.tool_count })}
                  </Badge>
                </Tip>
                {server.connected && server.platform_version && (
                  <span className="content-caption text-default-secondary font-mono-plex">
                    v{server.platform_version}
                  </span>
                )}
              </div>
              <p className="content-compact text-default-secondary">{server.description}</p>
            </div>
          </div>

          {server.connected ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-sm bg-elevation-hover px-3 py-2 content-compact font-mono-plex select-all truncate">
                {server.url}
              </code>
              <Tip content={t_i18n("Copy the endpoint URL")}>
                <Button
                  type="button"
                  variant="ghost"
                  className="shrink-0 gap-1.5"
                  data-testid="platform-mcp-copy-url"
                  aria-label={t_i18n("Copy the {server} MCP endpoint URL", { server: server.name })}
                  onClick={() => handleCopyUrl(server)}
                >
                  {copiedUrl === server.provider ? (
                    <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </Button>
              </Tip>
              {showConfig && (
                <Tip content={t_i18n("Copy a ready-to-paste MCP client configuration (Cursor, Claude Desktop, ...)")}>
                  <Button
                    type="button"
                    variant="outline"
                    className="shrink-0 gap-1.5"
                    data-testid="platform-mcp-copy-config"
                    onClick={() => handleCopySnippet(server)}
                  >
                    {copiedSnippet === server.provider ? (
                      <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                    ) : (
                      <Terminal className="h-3.5 w-3.5" />
                    )}
                    {t_i18n("Config")}
                  </Button>
                </Tip>
              )}
            </div>
          ) : (
            <p className="content-compact text-default-secondary rounded-sm bg-elevation-hover px-3 py-2">
              {t_i18n(
                "Activates automatically once a {server} platform is connected to {platform}. Ask your administrator to register it.",
                { server: server.name, platform: platformName || "XTM One" },
              )}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
