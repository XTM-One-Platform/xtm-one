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

import { useState } from "react";
import {
  AlertTriangle, Ban, KeyRound, Pencil, Plus, Server, Trash2, Wifi, WifiOff,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useDeleteRelay, useDeleteRelayEnrollmentKey, useGroupRelays, useRelayEnrollmentKeys,
  useRevokeRelay, useUpdateRelay,
} from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Relay, RelayGroup } from "@/types";
import { RELAY_LOGOS, relayPlatformKey, relayPlatformLabel } from "./relay-labels";
import { formatTimeAgo } from "@/lib/relative-time";

function RelayRow({ relay, canManage, groupId }: { relay: Relay; canManage: boolean; groupId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const update = useUpdateRelay();
  const revoke = useRevokeRelay();
  const remove = useDeleteRelay(groupId);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(relay.name);
  const platform = relayPlatformKey(relay);

  const saveName = () => {
    if (!name.trim() || name.trim() === relay.name) {
      setRenaming(false);
      return;
    }
    update.mutate({ id: relay.id, name: name.trim() }, { onSuccess: () => setRenaming(false) });
  };

  const onRevoke = async () => {
    const ok = await confirm({
      title: t_i18n("Revoke this relay?"),
      description: t_i18n("Its credential stops working at once and its connection is closed. Enroll it again to bring it back."),
      confirmLabel: t_i18n("Revoke"),
    });
    if (ok) revoke.mutate(relay.id, { onSuccess: () => toast({ title: t_i18n("Relay revoked"), variant: "success" }) });
  };

  const onDelete = async () => {
    const ok = await confirm({
      title: t_i18n("Delete this relay?"),
      description: t_i18n("It is removed from the group and its credential revoked. Its request log is kept."),
      confirmLabel: t_i18n("Delete"),
    });
    if (ok) remove.mutate(relay.id);
  };

  return (
    <div className="flex flex-wrap items-center gap-4 border-b border-elevation-subtle-soft px-4 py-3 last:border-0">
      {ConfirmEl}
      <div className="flex items-center gap-3 min-w-[14rem] flex-1">
        {relay.online ? (
          <Wifi className="h-4 w-4 text-feedback-success-primary shrink-0" />
        ) : relay.is_active ? (
          <WifiOff className="h-4 w-4 text-feedback-warning-primary shrink-0" />
        ) : (
          <Ban className="h-4 w-4 text-default-secondary shrink-0" />
        )}
        <div className="min-w-0">
          {renaming ? (
            <Input aria-label={t_i18n("Name")}
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onBlur={saveName}
              onKeyDown={(e) => {
                if (e.key === "Enter") saveName();
                if (e.key === "Escape") setRenaming(false);
              }}
              className="h-7 content-base"
            />
          ) : (
            <p className="flex items-center gap-1.5 content-base font-medium">
              {relay.name}
              {canManage && relay.is_active && (
                <button type="button" onClick={() => setRenaming(true)} aria-label={t_i18n("Rename")}>
                  <Pencil className="h-3 w-3 text-default-secondary hover:text-default-primary" />
                </button>
              )}
            </p>
          )}
          <p className="flex items-center gap-1.5 text-[11px] text-default-secondary">
            {RELAY_LOGOS[platform] && (
              <ProviderLogo provider={platform} logo={RELAY_LOGOS[platform]} className="h-3 w-3 shrink-0" decorative />
            )}
            {[relayPlatformLabel(platform) && t_i18n(relayPlatformLabel(platform)), relay.hostname, relay.key_prefix]
              .filter(Boolean)
              .join(" - ")}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        {relay.agent_version && (
          <Badge variant="outline" className={cn("content-caption", relay.outdated && "border-feedback-alert-primary/40 text-feedback-warning-primary")}>
            v{relay.agent_version}
          </Badge>
        )}
        {relay.outdated && (
          <Tip content={t_i18n("Older than the minimum supported version: update it (it updates itself unless it runs in a container).")}>
            <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary" />
          </Tip>
        )}
        {relay.transport && (
          <Badge variant="outline" className="content-caption">
            {relay.transport === "ws" ? t_i18n("WebSocket") : t_i18n("Long-poll")}
          </Badge>
        )}
        {relay.allows_all_destinations ? (
          <Tip content={t_i18n("Its local list allows every destination (*): it reaches anything its network reaches.")}>
            <Badge variant="outline" className="content-caption border-feedback-alert-primary/40 text-feedback-warning-primary">{t_i18n("Any destination")}</Badge>
          </Tip>
        ) : (
          <Tip content={relay.declared_destinations.join(", ") || t_i18n("Nothing: its local list is empty")}>
            <Badge variant="outline" className="content-caption">
              {t_i18n("{count, plural, one {# destination} other {# destinations}}", { count: relay.declared_destinations.length })}
            </Badge>
          </Tip>
        )}
      </div>

      <div className="flex items-center gap-4 text-[11px] text-default-secondary">
        <span className="tabular-nums">
          {t_i18n("{count, plural, one {# request} other {# requests}}", { count: relay.request_count })}
          {relay.inflight ? ` - ${t_i18n("{count} in flight", { count: relay.inflight })}` : ""}
        </span>
        <span>
          {relay.is_active
            ? relay.online
              ? t_i18n("Online")
              : relay.last_seen_at
                ? t_i18n("Last seen {when}", { when: formatTimeAgo(intl, relay.last_seen_at) })
                : t_i18n("Never connected")
            : t_i18n("Revoked")}
        </span>
      </div>

      {canManage && (
        <div className="flex items-center gap-1">
          {relay.is_active && (
            <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={onRevoke}>
              <Ban className="h-3.5 w-3.5 mr-1" /> {t_i18n("Revoke")}
            </Button>
          )}
          <Button variant="ghost" size="icon" className="h-7 w-7 text-feedback-error-primary" onClick={onDelete} aria-label={t_i18n("Delete")}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}

function EnrollmentKeysCard({ group }: { group: RelayGroup }) {
  const { t_i18n, intl } = useFormatter();
  const { data: keys } = useRelayEnrollmentKeys(group.id);
  const remove = useDeleteRelayEnrollmentKey(group.id);
  const active = (keys ?? []).filter((k) => k.is_active);
  if (active.length === 0) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 content-base">
          <KeyRound className="h-4 w-4 text-accent" /> {t_i18n("Enrollment keys")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {active.map((key) => (
          <div key={key.id} className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2 content-compact">
            <div className="min-w-0">
              <p className="font-medium truncate">{key.name}</p>
              <p className="text-[11px] text-default-secondary">
                <span className="font-mono-plex">{key.key_prefix}</span>
                {" - "}
                {key.max_uses == null
                  ? t_i18n("reusable, used {count} times", { count: key.use_count })
                  : t_i18n("used {used} of {max}", { used: key.use_count, max: key.max_uses })}
                {key.expires_at ? ` - ${t_i18n("expires {when}", { when: formatTimeAgo(intl, key.expires_at) })}` : ""}
              </p>
            </div>
            <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => remove.mutate(key.id)}>
              {t_i18n("Revoke")}
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

export function RelaysTab({ group, canManage, onEnroll }: { group: RelayGroup; canManage: boolean; onEnroll: () => void }) {
  const { t_i18n } = useFormatter();
  const [showRevoked, setShowRevoked] = useState(false);
  const { data: relays, isLoading } = useGroupRelays(group.id, showRevoked);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 content-compact text-default-secondary">
          <Switch checked={showRevoked} onCheckedChange={setShowRevoked} />
          {t_i18n("Show revoked relays")}
        </label>
        {canManage && (
          <Button size="sm" onClick={onEnroll}>
            <Plus className="h-4 w-4 mr-1" /> {t_i18n("Enroll a relay")}
          </Button>
        )}
      </div>
      {!isLoading && (relays?.length ?? 0) === 0 ? (
        <EmptyState
          icon={Server}
          title={t_i18n("No relay in this group")}
          description={t_i18n("Enroll a relay on a machine or in a container inside the network. Two or more give redundancy and share the load.")}
          actionLabel={canManage ? t_i18n("Enroll a relay") : undefined}
          onAction={canManage ? onEnroll : undefined}
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            {(relays ?? []).map((relay) => (
              <RelayRow key={relay.id} relay={relay} canManage={canManage} groupId={group.id} />
            ))}
          </CardContent>
        </Card>
      )}
      {canManage && <EnrollmentKeysCard group={group} />}
    </div>
  );
}
