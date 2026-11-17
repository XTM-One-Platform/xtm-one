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

import { Server, Wrench } from "lucide-react";
import { useParams } from "react-router-dom";

import { usePublicCatalogMCPServer } from "@/hooks/api/usePublicCatalogApi";
import { TRANSPORT_DISPLAY_NAMES } from "@/lib/constants";
import { useFormatter } from "@/i18n";

import { CatalogDetailSkeleton, DetailShell } from "./components";

export default function PublicMCPServerDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const { data: srv, isLoading } = usePublicCatalogMCPServer(id!);

  if (isLoading) return <CatalogDetailSkeleton backTo="/catalog/mcp-servers" backLabel="Back to MCP Servers" />;
  if (!srv) return <p className="text-center text-default-secondary py-20">{t_i18n("MCP server not found")}</p>;

  return (
    <DetailShell
      backTo="/catalog/mcp-servers"
      backLabel="Back to MCP Servers"
      entityType="mcp-servers"
      entityId={srv.id}
      icon={<Server className="h-6 w-6 text-accent" />}
      name={srv.name}
      description={srv.description ?? null}
      tags={srv.tags}
      createdAt={srv.created_at}
      updatedAt={srv.updated_at}
      sharedBy={srv.shared_by}
    >
      {/* Config */}
      <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-3">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
        <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">{t_i18n("Configuration")}</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="text-[11px] text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Transport")}</p>
            <p className="content-base font-medium">{TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport}</p>
          </div>
          {srv.command && (
            <div>
              <p className="text-[11px] text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Command")}</p>
              <p className="content-base font-mono-plex">{srv.command}</p>
            </div>
          )}
          {srv.url && (
            <div className="sm:col-span-2">
              <p className="text-[11px] text-default-secondary uppercase tracking-wider mb-1">{t_i18n("URL")}</p>
              <p className="content-base font-mono-plex break-all text-default-secondary">{srv.url}</p>
            </div>
          )}
          {srv.args && srv.args.length > 0 && (
            <div className="sm:col-span-2">
              <p className="text-[11px] text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Arguments")}</p>
              <p className="content-base font-mono-plex text-default-secondary">{srv.args.join(" ")}</p>
            </div>
          )}
        </div>
      </div>

      {/* Tools */}
      {srv.tools.length > 0 && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-3">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
            {t_i18n("Tools ({count})", { count: srv.tools.length })}
          </h2>
          <div className="divide-y divide-[var(--border-elevation-subtle)]">
            {srv.tools.map((t, i) => (
              <div key={i} className="py-2.5 first:pt-0 last:pb-0">
                <div className="flex items-center gap-2">
                  <Wrench className="h-3 w-3 text-accent/50" />
                  <span className="content-base font-medium font-mono-plex">{t.name}</span>
                </div>
                {t.description && (
                  <p className="content-compact text-default-secondary ml-5 mt-0.5">{t.description}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </DetailShell>
  );
}
