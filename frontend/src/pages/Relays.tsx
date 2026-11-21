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
 * Relays: the on-premises egress relays of this platform, by group.
 *
 * A relay group names a customer network (a datacenter, a zone). Its relays
 * run inside that network and connect OUT to the platform - nothing is ever
 * opened inbound - and the integrations, remote MCP servers and flow HTTP
 * steps routed through the group are sent from there. Groups are created by
 * admins; sharing decides who may route a resource through one.
 */
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowRight, Cable, Network, Plus, Route, Server, ShieldCheck, Workflow,
} from "lucide-react";

import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { SharedBadge } from "@/components/ui/shared-badge";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useCreateRelayGroup, useRelayGroups, useRelayMeta, useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn, isEntityShared } from "@/lib/utils";
import type { RelayGroup } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

export function RelayStatusDot({ online, total }: { online: number; total: number }) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-full shrink-0",
        online > 0 ? "bg-feedback-success-primary shadow-[0_0_8px] shadow-feedback-success-primary/60" : total > 0 ? "bg-feedback-alert-primary" : "bg-[var(--text-default-secondary)]",
      )}
    />
  );
}

function GroupCard({ group, userId }: { group: RelayGroup; userId?: string }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const restricted = group.allowed_destinations && group.allowed_destinations.length > 0;
  return (
    <Card
      className="cursor-pointer transition-all hover:bg-elevation-hover hover:shadow-glow h-full"
      onClick={() => navigate(`/connections/relays/${group.id}`)}
    >
      <CardContent className="p-4 flex flex-col gap-3 h-full">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <Network className="h-5 w-5 text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <p className="content-base font-medium truncate">{group.name}</p>
              {group.zone && <Badge variant="outline" className="content-caption">{group.zone}</Badge>}
            </div>
            <p className="text-[11px] text-default-secondary line-clamp-2">
              {group.description || t_i18n("No description")}
            </p>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {group.is_company_managed && <CompanyManagedBadge />}
            {isEntityShared(group, userId) && <SharedBadge />}
          </div>
        </div>
        <div className="mt-auto grid grid-cols-3 gap-2 border-t border-elevation-subtle-soft pt-3 text-[11px]">
          <div className="flex items-center gap-1.5">
            <RelayStatusDot online={group.relays_online} total={group.relays_total} />
            <span className="tabular-nums">
              {t_i18n("{online}/{total} online", { online: group.relays_online, total: group.relays_total })}
            </span>
          </div>
          <Tip content={t_i18n("Integrations, MCP servers and flows routed through this group")}>
            <div className="flex items-center gap-1.5 text-default-secondary">
              <Route className="h-3 w-3" />
              <span className="tabular-nums">
                {t_i18n("{count, plural, one {# routed} other {# routed}}", { count: group.routed_count })}
              </span>
            </div>
          </Tip>
          <div className="flex items-center gap-1.5 justify-end text-default-secondary">
            <ShieldCheck className={cn("h-3 w-3", restricted ? "text-feedback-success-primary" : "text-default-secondary")} />
            <span>
              {restricted
                ? t_i18n("{count, plural, one {# destination} other {# destinations}}", { count: group.allowed_destinations!.length })
                : t_i18n("Each relay's list")}
            </span>
          </div>
        </div>
        {group.updated_at && (
          <p className="content-caption text-default-secondary">{t_i18n("Updated {when}", { when: formatTimeAgo(intl, group.updated_at) })}</p>
        )}
      </CardContent>
    </Card>
  );
}

function HowItWorks() {
  const { t_i18n } = useFormatter();
  const steps = [
    { icon: Network, title: t_i18n("Create a group"), body: t_i18n("One group per network the platform cannot reach: a datacenter, a cloud account, a site.") },
    { icon: Server, title: t_i18n("Enroll a relay"), body: t_i18n("Install it on a machine or a container inside that network. It connects out over HTTPS: no inbound port, no VPN.") },
    { icon: Cable, title: t_i18n("Route resources"), body: t_i18n("Pick the group on an integration, an MCP server or a flow HTTP step: its requests then leave from that network.") },
  ];
  return (
    <div className="grid gap-3 md:grid-cols-3">
      {steps.map((step, i) => (
        <div key={step.title} className="flex gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent/10 content-compact font-semibold text-accent">
            {i + 1}
          </div>
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 content-compact font-medium">
              <step.icon className="h-3.5 w-3.5 text-accent" /> {step.title}
            </p>
            <p className="mt-0.5 text-[11px] text-default-secondary leading-relaxed">{step.body}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function CreateGroupDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const create = useCreateRelayGroup();
  const [name, setName] = useState("");
  const [zone, setZone] = useState("");
  const [description, setDescription] = useState("");
  const [destinations, setDestinations] = useState<string[]>([]);
  const [companyManaged, setCompanyManaged] = useState(true);

  const submit = () => {
    if (!name.trim()) return;
    create.mutate(
      {
        name: name.trim(),
        zone: zone.trim() || null,
        description: description.trim() || null,
        allowed_destinations: destinations.length > 0 ? destinations : null,
        is_company_managed: companyManaged,
      },
      {
        onSuccess: (group) => {
          toast({ title: t_i18n("Relay group created"), variant: "success" });
          onOpenChange(false);
          navigate(`/connections/relays/${group.id}?enroll=1`);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n("New relay group")}</DialogTitle>
          <DialogDescription>
            {t_i18n("A group names one network. Every relay enrolled in it can carry the requests routed through it.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="relay-group-name">{t_i18n("Name")}</Label>
              <Input id="relay-group-name" aria-label={t_i18n("Name")} value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("e.g. Paris datacenter")} autoFocus />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="relay-group-zone">{t_i18n("Zone")}</Label>
              <Input id="relay-group-zone" aria-label={t_i18n("Zone")} value={zone} onChange={(e) => setZone(e.target.value)} placeholder={t_i18n("e.g. eu-west, OT network")} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="relay-group-description">{t_i18n("Description")}</Label>
            <Input id="relay-group-description" aria-label={t_i18n("Description")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What this network holds (Splunk, Tanium, ...)")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="relay-group-destinations">{t_i18n("Allowed destinations")}</Label>
            <TagInput
              id="relay-group-destinations"
              value={destinations}
              onChange={setDestinations}
              placeholder={t_i18n("host:port, *.corp.local:443, 10.20.0.0/16:8089")}
            />
            <p className="text-[11px] text-default-secondary">
              {t_i18n("Optional platform-side limit. Each relay also enforces its own local list, which always has the last word.")}
            </p>
          </div>
          <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
            <div>
              <p className="content-base font-medium">{t_i18n("Company-managed")}</p>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Every user may route their resources through it. Turn off to share it with groups or people instead.")}
              </p>
            </div>
            <Switch aria-label={t_i18n("Company-managed")} checked={companyManaged} onCheckedChange={setCompanyManaged} />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t_i18n("Cancel")}</Button>
            <Button onClick={submit} disabled={!name.trim() || create.isPending}>
              {t_i18n("Create and enroll a relay")} <ArrowRight className="h-4 w-4 ml-1" />
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function Relays() {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const enabled = useRelaysEnabled();
  const { data: groups, isLoading } = useRelayGroups({ enabled: enabled === true });
  const { data: meta } = useRelayMeta({ enabled: enabled === true });
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return groups ?? [];
    return (groups ?? []).filter((g) =>
      [g.name, g.zone ?? "", g.description ?? "", ...(g.tags ?? [])].some((v) => v.toLowerCase().includes(term)),
    );
  }, [groups, search]);

  const totals = useMemo(() => {
    const list = groups ?? [];
    return {
      online: list.reduce((n, g) => n + g.relays_online, 0),
      total: list.reduce((n, g) => n + g.relays_total, 0),
      routed: list.reduce((n, g) => n + g.routed_count, 0),
    };
  }, [groups]);

  // While the flag or the first read is in flight, the family header stays mounted and
  // the groups load as cards on the loaded grid (#11345).
  if (enabled === undefined || (enabled && isLoading)) {
    return (
      <div className="space-y-6">
        <NavFamilyHeader
          to="/connections/relays"
          title="Relays"
          infoLabel={t_i18n("About relays")}
          info={t_i18n("Reach the services inside your networks - Splunk, Tanium, an on-premises OpenCTI - without opening any inbound flow. A relay runs in the network and connects out to the platform.")}
        />
        <CardGridSkeleton count={cardSkeletonCount()} label={t_i18n("Loading")} className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" />
      </div>
    );
  }
  if (!enabled) {
    return (
      <div className="space-y-6">
        {/* Relays off: the page is no member of the reader's family, so the
            shared header shows its own title and no tab row. */}
        <NavFamilyHeader
          to="/connections/relays"
          title="Relays"
          infoLabel={t_i18n("About relays")}
          info={t_i18n("Reach the services inside your networks - Splunk, Tanium, an on-premises OpenCTI - without opening any inbound flow. A relay runs in the network and connects out to the platform.")}
        />
        <EmptyState
          icon={Network}
          title={t_i18n("Relays are turned off")}
          description={t_i18n("RELAYS_ENABLED=false on this platform. Remove it to reach services inside networks the platform cannot reach directly.")}
        />
      </div>
    );
  }

  const canCreate = !!meta?.can_create_groups;

  return (
    <div className="space-y-6">
      {/* The Connections family header (#9301 N3b): its h1, the page's help
          in the "i" (#10215), and one tab row of the connection pages. */}
      <NavFamilyHeader
        to="/connections/relays"
        title="Relays"
        infoLabel={t_i18n("About relays")}
        info={t_i18n("Reach the services inside your networks - Splunk, Tanium, an on-premises OpenCTI - without opening any inbound flow. A relay runs in the network and connects out to the platform.")}
        actions={canCreate ? (
          <Button onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4 mr-1" /> {t_i18n("New relay group")}
          </Button>
        ) : undefined}
      />
      <CreateGroupDialog open={creating} onOpenChange={setCreating} />

      {(groups?.length ?? 0) === 0 ? (
        <div className="space-y-4">
          <HowItWorks />
          <EmptyState
            icon={Network}
            title={t_i18n("No relay group yet")}
            description={
              canCreate
                ? t_i18n("Create a group for the first network you need to reach, then enroll a relay in it.")
                : t_i18n("An administrator creates relay groups. Once one is shared with you, you can route your integrations through it.")
            }
            actionLabel={canCreate ? t_i18n("New relay group") : undefined}
            onAction={canCreate ? () => setCreating(true) : undefined}
          />
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-4 content-compact text-default-secondary">
            <span className="flex items-center gap-1.5">
              <RelayStatusDot online={totals.online} total={totals.total} />
              {t_i18n("{online} of {total} relays online", { online: totals.online, total: totals.total })}
            </span>
            <span className="flex items-center gap-1.5">
              <Workflow className="h-3.5 w-3.5" />
              {t_i18n("{count, plural, one {# resource routed} other {# resources routed}}", { count: totals.routed })}
            </span>
          </div>
          <SearchInput value={search} onChange={setSearch} placeholder={t_i18n("Search relay groups...")} className="max-w-sm" />
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 stagger-children">
            {filtered.map((group) => (
              <GroupCard key={group.id} group={group} userId={user?.id} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
