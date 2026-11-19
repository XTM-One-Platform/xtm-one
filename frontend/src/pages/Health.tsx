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
 * The Platform Health page for administrators (#8261, PRD 6.11; renamed #101 S1): every improvement
 * proposal (search, type and status filters, sort), every cycle with its digest, and the
 * signals currently crossed. Built in since #101 (S1); managers find their
 * own targets' proposals and cycles on the agent and flow pages.
 */
import { Link } from "react-router-dom";
import { Activity, Compass, Lightbulb, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { HEALTH_SETTINGS_PATH } from "@/pages/settings/settings-routes";
import { HealthCyclesList } from "@/components/health/HealthCyclesList";
import { HealthSignalsList } from "@/components/health/HealthSignalsList";
import { ImprovementsInbox } from "@/components/health/ImprovementsInbox";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";

type Tab = "improvements" | "cycles" | "signals";

export default function Health() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<Tab>("improvements");
  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "improvements", label: "Improvements", icon: Lightbulb },
    { key: "cycles", label: "Cycles", icon: Compass },
    { key: "signals", label: "Signals", icon: Activity },
  ];
  return (
    <div className="space-y-6">
      {/* One header row (#10215); the title is the product name, Platform
          Health (#101 S1). Its settings are one click away (#9301 N4: the
          page and the setting link to each other). */}
      <ListPageHeader
        title={t_i18n("Platform Health")}
        infoLabel={t_i18n("About Platform Health")}
        info={t_i18n("What Platform Health found, tried and proposes, cycle by cycle, and the signals it watches.")}
        actions={
          <Button variant="outline" className="gap-1.5" asChild>
            <Link to={HEALTH_SETTINGS_PATH}>
              <Settings2 className="h-4 w-4" /> {t_i18n("Platform Health settings")}
            </Link>
          </Button>
        }
      />
      <DetailTabs tabs={tabs} active={tab} />
      {tab === "improvements" && <ImprovementsInbox />}
      {tab === "cycles" && <HealthCyclesList />}
      {tab === "signals" && <HealthSignalsList />}
    </div>
  );
}
