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

import { Activity, AlertTriangle, ArrowDownUp, Gauge, Route, Server, ShieldCheck, Timer } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { Tip } from "@/components/ui/tooltip";
import { useGroupRelays, useRelayGroupStats } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { cn, formatShortDateTime } from "@/lib/utils";
import type { RelayGroup } from "@/types";
import { formatBytes } from "./relay-labels";

function RequestChart({ buckets }: { buckets: { at: string | null; requests: number; errors: number }[] }) {
  const { t_i18n } = useFormatter();
  const max = Math.max(1, ...buckets.map((b) => b.requests));
  if (buckets.length === 0) {
    return <p className="py-8 text-center content-compact text-default-secondary">{t_i18n("No request in this period.")}</p>;
  }
  return (
    <div className="flex h-28 items-end gap-1">
      {buckets.map((b, i) => {
        const okHeight = ((b.requests - b.errors) / max) * 100;
        const errHeight = (b.errors / max) * 100;
        return (
          <Tip
            key={b.at ?? i}
            content={t_i18n("{when}: {requests, plural, one {# request} other {# requests}}, {errors, plural, one {# error} other {# errors}}", {
              when: b.at ? formatShortDateTime(b.at) : "",
              requests: b.requests,
              errors: b.errors,
            })}
          >
            <div className="flex h-full flex-1 flex-col justify-end">
              {b.errors > 0 && <div className="w-full rounded-t-sm bg-feedback-error-primary/70" style={{ height: `${errHeight}%` }} />}
              <div
                className={cn("w-full bg-accent/60", b.errors > 0 ? "" : "rounded-t-sm")}
                style={{ height: `${okHeight}%` }}
              />
            </div>
          </Tip>
        );
      })}
    </div>
  );
}

export function OverviewTab({
  group,
  canManage,
  onEnroll,
}: {
  group: RelayGroup;
  canManage: boolean;
  onEnroll: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: stats } = useRelayGroupStats(group.id, 24);
  const { data: relays } = useGroupRelays(group.id);
  const outdated = (relays ?? []).filter((r) => r.outdated).length;
  const allowsAll = (relays ?? []).filter((r) => r.allows_all_destinations).length;
  const errorRate = stats && stats.requests > 0 ? Math.round((stats.errors / stats.requests) * 100) : 0;

  return (
    <div className="space-y-6">
      {group.relays_total === 0 && (
        <Card className="border-accent/30 bg-accent/[0.03]">
          <CardContent className="flex flex-wrap items-center justify-between gap-4 p-5">
            <div className="flex items-start gap-3">
              <Server className="h-5 w-5 text-accent mt-0.5" />
              <div>
                <p className="content-base font-medium">{t_i18n("No relay enrolled yet")}</p>
                <p className="content-compact text-default-secondary">
                  {t_i18n("Requests routed through this group fail until a relay of the group is online.")}
                </p>
              </div>
            </div>
            {canManage && <Button onClick={onEnroll}>{t_i18n("Enroll a relay")}</Button>}
          </CardContent>
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={Server}
          label={t_i18n("Relays online")}
          value={`${group.relays_online}/${group.relays_total}`}
          subtitle={group.relays_online > 1 ? t_i18n("Requests are balanced across them") : t_i18n("Enroll a second relay for redundancy")}
        />
        <StatCard
          icon={Activity}
          label={t_i18n("Requests (24h)")}
          value={stats?.requests ?? 0}
          subtitle={t_i18n("{rate}% failed", { rate: errorRate })}
          iconClassName={errorRate > 10 ? "text-feedback-error-primary" : undefined}
        />
        <StatCard
          icon={Timer}
          label={t_i18n("Average latency")}
          value={stats?.avg_duration_ms != null ? `${Math.round(stats.avg_duration_ms)} ms` : "-"}
          subtitle={t_i18n("Platform to destination and back")}
        />
        <StatCard
          icon={ArrowDownUp}
          label={t_i18n("Traffic (24h)")}
          value={formatBytes((stats?.bytes_sent ?? 0) + (stats?.bytes_received ?? 0))}
          subtitle={t_i18n("{sent} sent, {received} received", {
            sent: formatBytes(stats?.bytes_sent ?? 0),
            received: formatBytes(stats?.bytes_received ?? 0),
          })}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 content-base">
              <Gauge className="h-4 w-4 text-accent" /> {t_i18n("Requests over the last 24 hours")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <RequestChart buckets={stats?.buckets ?? []} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 content-base">
              <ShieldCheck className="h-4 w-4 text-accent" /> {t_i18n("Destination policy")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 content-compact">
            {group.allowed_destinations && group.allowed_destinations.length > 0 ? (
              <div className="space-y-1.5">
                <p className="text-default-secondary">{t_i18n("The platform only sends requests for:")}</p>
                <div className="flex flex-wrap gap-1">
                  {group.allowed_destinations.map((d) => (
                    <Badge key={d} variant="outline" className="font-mono-plex content-caption">{d}</Badge>
                  ))}
                </div>
              </div>
            ) : (
              <p className="text-default-secondary">
                {t_i18n("No platform-side limit: each relay's own list decides what it reaches.")}
              </p>
            )}
            <p className="flex items-center gap-1.5 text-default-secondary">
              <Route className="h-3.5 w-3.5" />
              {t_i18n("{count, plural, one {# resource routed through this group} other {# resources routed through this group}}", { count: group.routed_count })}
            </p>
            {allowsAll > 0 && (
              <p className="flex items-start gap-1.5 text-feedback-warning-primary">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                {t_i18n("{count, plural, one {# relay allows every destination (*): it reaches anything its network reaches.} other {# relays allow every destination (*): they reach anything their network reaches.}}", { count: allowsAll })}
              </p>
            )}
            {outdated > 0 && (
              <p className="flex items-start gap-1.5 text-feedback-warning-primary">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                {t_i18n("{count, plural, one {# relay runs an unsupported version and must be updated.} other {# relays run an unsupported version and must be updated.}}", { count: outdated })}
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
