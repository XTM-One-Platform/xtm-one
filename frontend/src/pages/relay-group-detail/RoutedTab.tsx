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

import { Link } from "react-router-dom";
import { EyeOff, Link2, Route, Server, Workflow } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { useRelayGroupRouted } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import type { RelayGroup, RelayRoutedEntry } from "@/types";

function Section({
  title,
  icon: Icon,
  entries,
  href,
  provider,
}: {
  title: string;
  icon: typeof Server;
  entries: RelayRoutedEntry[];
  href: (id: string) => string;
  provider?: boolean;
}) {
  if (entries.length === 0) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 content-base">
          <Icon className="h-4 w-4 text-accent" /> {title}
          <span className="text-default-secondary font-normal">({entries.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 sm:grid-cols-2">
        {entries.map((entry) => (
          <Link
            key={entry.id}
            to={href(entry.id)}
            className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft px-3 py-2 content-base transition-colors hover:bg-elevation-hover"
          >
            {provider && entry.provider ? (
              <ProviderLogo provider={entry.provider} className="h-5 w-5" />
            ) : (
              <Icon className="h-4 w-4 text-default-secondary" />
            )}
            <span className="min-w-0 flex-1 truncate">{entry.name}</span>
            {provider && entry.provider && (
              <span className="text-[11px] text-default-secondary">
                {INTEGRATION_PROVIDER_NAMES[entry.provider] ?? entry.provider}
              </span>
            )}
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}

export function RoutedTab({ group }: { group: RelayGroup }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useRelayGroupRouted(group.id);
  if (isLoading || !data) return null;
  if (data.total === 0) {
    return (
      <EmptyState
        icon={Route}
        title={t_i18n("Nothing is routed through this group yet")}
        description={t_i18n("Open an integration, an MCP server or a flow HTTP step and choose this group under Route through.")}
      />
    );
  }
  return (
    <div className="space-y-4">
      <Section title={t_i18n("Integrations")} icon={Link2} entries={data.integrations} href={(id) => `/integrations/${id}`} provider />
      <Section title={t_i18n("MCP servers")} icon={Server} entries={data.mcp_servers} href={(id) => `/mcp-servers/${id}`} />
      <Section title={t_i18n("Flows")} icon={Workflow} entries={data.flows} href={(id) => `/flows/library/${id}`} />
      {data.hidden > 0 && (
        <p className="flex items-center gap-1.5 content-compact text-default-secondary">
          <EyeOff className="h-3.5 w-3.5" />
          {t_i18n("{count, plural, one {# more resource you cannot see is routed through this group.} other {# more resources you cannot see are routed through this group.}}", { count: data.hidden })}
        </p>
      )}
    </div>
  );
}
