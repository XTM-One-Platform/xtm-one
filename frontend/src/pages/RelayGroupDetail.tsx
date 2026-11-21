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
 * One relay group: its relays, what is routed through it, the
 * requests it carried, a destination test from its network, and its settings.
 */
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Activity, FileText, Network, Plus, Route, Server, Settings, Stethoscope,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Button } from "@/components/ui/button";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { NotFoundState } from "@/components/ui/not-found-state";
import { SharedBadge } from "@/components/ui/shared-badge";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useRelayGroup } from "@/hooks/api/useRelayApi";
import { useAuth } from "@/hooks/useAuth";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { canManageEntity, isEntityShared } from "@/lib/utils";
import { RelayStatusDot } from "@/pages/Relays";
import { ActivityTab } from "./relay-group-detail/ActivityTab";
import { DiagnosticsTab } from "./relay-group-detail/DiagnosticsTab";
import { EnrollDialog } from "./relay-group-detail/EnrollDialog";
import { OverviewTab } from "./relay-group-detail/OverviewTab";
import { RelaysTab } from "./relay-group-detail/RelaysTab";
import { RoutedTab } from "./relay-group-detail/RoutedTab";
import { SettingsTab } from "./relay-group-detail/SettingsTab";

type Tab = "overview" | "relays" | "routed" | "activity" | "diagnostics" | "settings";

const TABS: { key: Tab; label: string; icon: React.ElementType }[] = [
  { key: "overview", label: "Overview", icon: FileText },
  { key: "relays", label: "Relays", icon: Server },
  { key: "routed", label: "Routed resources", icon: Route },
  { key: "activity", label: "Activity", icon: Activity },
  { key: "diagnostics", label: "Diagnostics", icon: Stethoscope },
  { key: "settings", label: "Settings", icon: Settings },
];

export default function RelayGroupDetail() {
  const { t_i18n } = useFormatter();
  const { id = "" } = useParams<{ id: string; tab?: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab] = useTabParam<Tab>("overview");
  const { user, isAdmin } = useAuth();
  const { data: group, isLoading } = useRelayGroup(id);
  const [enrolling, setEnrolling] = useState(false);

  // A group just created lands here with ?enroll=1: open the wizard once.
  useEffect(() => {
    if (group && searchParams.get("enroll") === "1") {
      setEnrolling(true);
      searchParams.delete("enroll");
      setSearchParams(searchParams, { replace: true });
    }
  }, [group, searchParams, setSearchParams]);

  if (isLoading) return <DetailPageSkeleton backTo="/connections/relays" backLabel="Back to Relays" tabs={TABS.length} actions={1} />;
  if (!group) {
    return (
      <NotFoundState
        icon={Network}
        title="Relay group not found"
        description="This relay group may have been deleted, or it is not shared with you."
        actionLabel="View all relay groups"
        onAction={() => navigate("/connections/relays")}
        backTo="/connections/relays"
        backLabel="Back to Relays"
      />
    );
  }

  const canManage = canManageEntity(group, user?.id, isAdmin);
  const enroll = () => setEnrolling(true);

  return (
    <div className="space-y-6">
      <EnrollDialog group={group} open={enrolling} onOpenChange={setEnrolling} />
      <EntityPageHeader
        backTo="/connections/relays"
        backLabel="Back to Relays"
        icon={Network}
        title={group.name}
        badges={
          (group.zone || group.is_company_managed || isEntityShared(group, user?.id)) && (
            <>
              {group.zone && <Badge variant="outline">{group.zone}</Badge>}
              {group.is_company_managed && <CompanyManagedBadge size="default" />}
              {isEntityShared(group, user?.id) && <SharedBadge size="default" />}
            </>
          )
        }
        meta={
          <span className="flex items-center gap-2 content-base">
            <RelayStatusDot online={group.relays_online} total={group.relays_total} />
            {t_i18n("{online} of {total} relays online", { online: group.relays_online, total: group.relays_total })}
            <span className="text-default-disabled">|</span>
            {t_i18n("{count, plural, one {# resource routed} other {# resources routed}}", { count: group.routed_count })}
          </span>
        }
        actions={
          canManage && (
            <Button onClick={enroll}>
              <Plus className="h-4 w-4 mr-1" /> {t_i18n("Enroll a relay")}
            </Button>
          )
        }
      />

      <DetailTabs tabs={TABS} active={tab} />

      {tab === "overview" && <OverviewTab group={group} canManage={canManage} onEnroll={enroll} />}
      {tab === "relays" && <RelaysTab group={group} canManage={canManage} onEnroll={enroll} />}
      {tab === "routed" && <RoutedTab group={group} />}
      {tab === "activity" && <ActivityTab group={group} />}
      {tab === "diagnostics" && <DiagnosticsTab group={group} canManage={canManage} />}
      {tab === "settings" && <SettingsTab group={group} canManage={canManage} />}
    </div>
  );
}
