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
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { Input } from "@/components/ui/input";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { useDeleteRelayGroup, useUpdateRelayGroup } from "@/hooks/api/useRelayApi";
import { useAuth } from "@/hooks/useAuth";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import type { RelayGroup } from "@/types";

export function SettingsTab({ group, canManage }: { group: RelayGroup; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const update = useUpdateRelayGroup();
  const remove = useDeleteRelayGroup();
  const [ConfirmEl, confirm] = useConfirm();
  const shareGrants = useUserShareGrants(group);

  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(group.name);
  const [zone, setZone] = useState(group.zone ?? "");
  const [description, setDescription] = useState(group.description ?? "");
  const [destinations, setDestinations] = useState<string[]>(group.allowed_destinations ?? []);
  const [tags, setTags] = useState<string[]>(group.tags ?? []);
  const [companyManaged, setCompanyManaged] = useState(group.is_company_managed);
  const [sharedGroupIds, setSharedGroupIds] = useState<string[] | null>(group.shared_group_ids ?? null);
  const [sharedGroupPermissions, setSharedGroupPermissions] = useState<Record<string, string> | null>(
    group.shared_group_permissions ?? null,
  );

  const start = () => {
    setName(group.name);
    setZone(group.zone ?? "");
    setDescription(group.description ?? "");
    setDestinations(group.allowed_destinations ?? []);
    setTags(group.tags ?? []);
    setCompanyManaged(group.is_company_managed);
    setSharedGroupIds(group.shared_group_ids ?? null);
    setSharedGroupPermissions(group.shared_group_permissions ?? null);
    shareGrants.reset();
    setEditing(true);
  };

  const save = () => {
    update.mutate(
      {
        id: group.id,
        body: {
          name: name.trim(),
          zone: zone.trim() || null,
          description: description.trim() || null,
          allowed_destinations: destinations.length > 0 ? destinations : null,
          tags,
          ...(isAdmin && companyManaged !== group.is_company_managed ? { is_company_managed: companyManaged } : {}),
          shared_group_ids: companyManaged ? null : sharedGroupIds && sharedGroupIds.length > 0 ? sharedGroupIds : null,
          shared_group_permissions: companyManaged ? null : sharedGroupPermissions,
          ...shareGrants.toPayload(companyManaged),
        },
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Relay group updated"), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  const onDelete = async () => {
    const ok = await confirm({
      title: t_i18n("Delete this relay group?"),
      description: t_i18n("Its relays are revoked at once. A group something is still routed through cannot be deleted: route those resources elsewhere first."),
      confirmLabel: t_i18n("Delete"),
    });
    if (!ok) return;
    // A group still in use answers 409 with how many resources route through
    // it; the global error toast says so.
    remove.mutate(group.id, { onSuccess: () => navigate("/connections/relays") });
  };

  return (
    <div className="space-y-6">
      {ConfirmEl}
      <EntityConfigCard
        entityLabel="relay group"
        isCompanyManaged={group.is_company_managed}
        userId={group.user_id}
        sharedGroupIds={group.shared_group_ids}
        canManage={canManage}
        editing={editing}
        onStartEditing={start}
        onCancel={() => setEditing(false)}
        onSave={save}
        isSaving={update.isPending}
        requiresAi={false}
        canSave={!!name.trim()}
        editCompanyManaged={companyManaged}
        onCompanyManagedChange={setCompanyManaged}
        editSharedGroupIds={sharedGroupIds}
        onSharedGroupIdsChange={setSharedGroupIds}
        editSharedGroupPermissions={sharedGroupPermissions}
        onSharedGroupPermissionsChange={setSharedGroupPermissions}
        userSharing={shareGrants.props}
        companyManagedDescription={
          companyManaged
            ? t_i18n("Every user may route their resources through this group.")
            : t_i18n("Only you and the groups or people it is shared with may route resources through it.")
        }
        readView={
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</p>
              <p className="content-base font-medium">{group.name}</p>
            </div>
            <div>
              <p className="content-compact text-default-secondary mb-1">{t_i18n("Zone")}</p>
              <p className="content-base">{group.zone || "-"}</p>
            </div>
            <div className="sm:col-span-2">
              <p className="content-compact text-default-secondary mb-1">{t_i18n("Description")}</p>
              <p className="content-base text-default-secondary">{group.description || "-"}</p>
            </div>
            <div className="sm:col-span-2">
              <p className="content-compact text-default-secondary mb-1">{t_i18n("Allowed destinations")}</p>
              {group.allowed_destinations && group.allowed_destinations.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {group.allowed_destinations.map((d) => (
                    <Badge key={d} variant="outline" className="font-mono-plex content-caption">{d}</Badge>
                  ))}
                </div>
              ) : (
                <p className="content-base text-default-secondary">{t_i18n("No platform-side limit: each relay's own list decides.")}</p>
              )}
            </div>
          </div>
        }
        editView={
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label className="content-compact text-default-secondary">{t_i18n("Name")}</label>
                <Input aria-label={t_i18n("Name")} value={name} onChange={(e) => setName(e.target.value)} className="h-8 content-base" />
              </div>
              <div className="space-y-1">
                <label className="content-compact text-default-secondary">{t_i18n("Zone")}</label>
                <Input aria-label={t_i18n("Zone")} value={zone} onChange={(e) => setZone(e.target.value)} className="h-8 content-base" />
              </div>
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Description")}</label>
              <Input aria-label={t_i18n("Description")} value={description} onChange={(e) => setDescription(e.target.value)} className="h-8 content-base" />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Allowed destinations")}</label>
              <TagInput
                value={destinations}
                onChange={setDestinations}
                placeholder={t_i18n("host:port, *.corp.local:443, 10.20.0.0/16:8089")}
              />
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Empty: no platform-side limit. With a list, a request to anything not on it is refused before it reaches a relay.")}
              </p>
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Tags")}</label>
              <TagInput value={tags} onChange={setTags} placeholder={t_i18n("Add tags…")} />
            </div>
          </div>
        }
      />

      {canManage && (
        <Card className="border-feedback-error-primary/30">
          <CardHeader className="pb-2">
            <CardTitle className="content-base text-feedback-error-primary">{t_i18n("Delete this group")}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center justify-between gap-3">
            <p className="content-compact text-default-secondary max-w-xl">
              {t_i18n("Revokes every relay of the group. Refused while an integration, an MCP server or a flow is routed through it: a routed resource never falls back to direct.")}
            </p>
            <Button variant="ghost" className="text-feedback-error-primary hover:text-feedback-error-primary" onClick={onDelete} disabled={remove.isPending}>
              <Icon name="trash-2" size={16} className="mr-1" /> {t_i18n("Delete")}
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
