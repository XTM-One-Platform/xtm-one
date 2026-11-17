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

import {
  Radio,
  Info,
  CheckCircle2,
  Bot,
  Wrench,
  Zap,
  Activity,
  Lock,
  AlertCircle,
  Tag,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { AgentsUsingCard } from "@/components/ui/agents-using-card";
import { ResourceSharingCard } from "@/components/ui/resource-sharing-card";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { useAgents } from "@/hooks/api/useAgentApi";
import type { MCPServer } from "@/types";
import { formatShortDate, formatTime, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";

// ---------------------------------------------------------------------------
// Overview Card
// ---------------------------------------------------------------------------

function OverviewCard({ srv }: { srv: MCPServer }) {
  const { t_i18n } = useFormatter();
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Radio className="h-4 w-4 text-accent" />
          {t_i18n("Overview")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Connection details */}
        <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
          <div className="content-compact font-medium text-default-secondary">{t_i18n("Connection Details")}</div>
          {srv.transport === "stdio" ? (
            <>
              {srv.command && (
                <div className="flex items-start gap-2">
                  <span className="content-caption text-default-secondary uppercase tracking-wider w-16 shrink-0 pt-0.5">{t_i18n("Command")}</span>
                  <code className="content-compact font-mono-plex text-default-secondary break-all">{srv.command}</code>
                </div>
              )}
              {srv.args && srv.args.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="content-caption text-default-secondary uppercase tracking-wider w-16 shrink-0 pt-0.5">{t_i18n("Args")}</span>
                  <code className="content-compact font-mono-plex text-default-secondary break-all">{srv.args.join(" ")}</code>
                </div>
              )}
            </>
          ) : (
            srv.url && (
              <div className="flex items-start gap-2">
                <span className="content-caption text-default-secondary uppercase tracking-wider w-20 shrink-0 pt-0.5">{srv.transport === "streamable_http" ? t_i18n("Endpoint") : t_i18n("URL")}</span>
                <code className="content-compact font-mono-plex text-accent break-all">{srv.url}</code>
              </div>
            )
          )}
          <div className="flex items-center gap-3 pt-1 flex-wrap">
            {srv.has_env && (
              <div className="flex items-center gap-1.5">
                <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />
                <span className="content-compact text-default-secondary">{t_i18n("Environment variables set")}</span>
              </div>
            )}
            {srv.auth_mode === "headers" && srv.has_headers && (
              <div className="flex items-center gap-1.5">
                <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />
                <span className="content-compact text-default-secondary">{t_i18n("Custom headers set")}</span>
              </div>
            )}
            {srv.auth_mode === "oauth" && (
              <div className="flex items-center gap-1.5">
                <Lock className="h-3 w-3 text-accent" />
                <span className="content-compact text-default-secondary">OAuth 2.0</span>
                {srv.oauth_status === "connected" ? (
                  <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />
                ) : srv.oauth_status === "error" ? (
                  <AlertCircle className="h-3 w-3 text-feedback-error-primary" />
                ) : (
                  <AlertCircle className="h-3 w-3 text-feedback-warning-primary" />
                )}
                <span className="content-compact text-default-secondary">
                  {srv.oauth_status === "connected" ? t_i18n("Connected") : srv.oauth_status === "error" ? t_i18n("Error") : t_i18n("Not connected")}
                </span>
              </div>
            )}
            {!srv.has_env && srv.auth_mode !== "oauth" && !srv.has_headers && (
              <div className="flex items-center gap-1.5">
                <Info className="h-3 w-3 text-default-secondary" />
                <span className="content-compact text-default-secondary">{t_i18n("No secrets configured")}</span>
              </div>
            )}
          </div>
          <VaultSecretsNotice status={srv.vault_secrets} className="pt-1" />
        </div>

        {/* Tags */}
        {srv.tags && srv.tags.length > 0 && (
          <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
            <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5"><Tag className="h-3 w-3" />{t_i18n("Tags")}</div>
            <div className="flex flex-wrap gap-1.5">
              {srv.tags.map((tag) => (
                <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
              ))}
            </div>
          </div>
        )}

        {/* Timestamps */}
        <TimestampGrid createdAt={srv.created_at} updatedAt={srv.updated_at} />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Stats Card (KPI row with icons)
// ---------------------------------------------------------------------------

function StatsCard({ srv }: { srv: MCPServer }) {
  const { t_i18n } = useFormatter();
  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.mcp_server_ids?.includes(srv.id)) || [];

  const lastCalled = srv.last_called_at ? new Date(srv.last_called_at) : null;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard icon={Bot} label="Agents Using" value={boundAgents.length} subtitle={t_i18n("{count, plural, one {agent connected} other {agents connected}}", { count: boundAgents.length })} />
      <StatCard icon={Wrench} label="Tools" value={srv.tool_count ?? "—"} subtitle={srv.tool_count != null ? "discovered" : "not yet discovered"} />
      <StatCard icon={Zap} label="Tool Calls" value={srv.call_count} subtitle={t_i18n("across {unit} connected", { unit: t_i18n(pluralize(boundAgents.length, "agent")) })} />
      <StatCard icon={Activity} label="Last Activity" value={lastCalled ? formatShortDate(lastCalled) : "—"} subtitle={lastCalled ? formatTime(lastCalled) : "No calls yet"} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Overview
// ---------------------------------------------------------------------------

export function OverviewTab({ srv }: { srv: MCPServer }) {
  const { data: agents } = useAgents({ scope: "all" });
  const boundAgents = agents?.filter((a) => a.mcp_server_ids?.includes(srv.id)) || [];

  return (
    <div className="space-y-6">
      <StatsCard srv={srv} />
      <OverviewCard srv={srv} />
      <AgentsUsingCard agents={boundAgents} entityLabel="Server" emptyHint="Bind this server to an agent in the agent's settings." />
      {/* To whom it is shared and through what: its managers only. */}
      <ResourceSharingCard kind="mcp_server" id={srv.id} enabled={srv.can_manage !== false} />
    </div>
  );
}
