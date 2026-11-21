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
 * /desktop/runners/:id - one enrolled machine.
 *
 * The XTM One Desktop page lists the machines YOU can see - own, shared with
 * you, company-managed - admins included. The by-id route keeps the
 * admin bypass, and this page is what mounts it: the admin Objects browser
 * opens a colleague's machine here, and a machine's URL can be handed around.
 * The card is the Desktop page's own `RunnerCard`, expanded, so host facts,
 * the permission model, pending approvals and the manage actions `can_manage`
 * allows are the same on both surfaces.
 */
import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { MonitorSmartphone } from "lucide-react";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useRunner } from "@/hooks/api/useRunnerApi";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { RunnerCard } from "@/pages/desktop/RunnersSection";
import RouteNotFound from "@/pages/RouteNotFound";
import { useFormatter } from "@/i18n";

export default function RunnerDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { t_i18n } = useFormatter();
  // `RUNNERS_ENABLED=false` removes the Desktop page and 404s every runner
  // route; this page has nothing to show either.
  const runnersEnabled = useRunnersEnabled();
  // Requested while the flag is not known yet, alongside `/auth/config`: with
  // runners off the 404 is an ordinary answer, and nothing renders before the
  // flag is known.
  const { data: runner, isLoading } = useRunner(runnersEnabled !== false ? id : undefined, { tolerateNotFound: true });

  // Contextual back navigation: the admin Objects browser sends the admin back
  // to itself; a direct link lands on the machines' home page.
  const locState = location.state as { backTo?: string; backLabel?: string } | null;
  const backTo = locState?.backTo ?? "/desktop";
  const backLabel = locState?.backLabel ?? "Back to XTM One Desktop";

  if (runnersEnabled === false) return <Navigate to="/" replace />;

  // Not known yet (`undefined`): wait, a redirect now would bounce a deep link.
  if (runnersEnabled === undefined || isLoading) {
    return <DetailPageSkeleton backTo={backTo} backLabel={backLabel} tabs={0} />;
  }

  // An unknown id (or one you cannot read) is an address no page answers (#797 S4).
  if (!runner) return <RouteNotFound />;

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={backTo}
        backLabel={backLabel}
        icon={MonitorSmartphone}
        title={runner.name}
        badges={
          <>
            <span className="content-base text-default-secondary">{t_i18n("Enrolled machine")}</span>
            {runner.is_company_managed && <CompanyManagedBadge size="default" />}
          </>
        }
      />

      {/* Deleting the machine leaves its now-dead route for wherever the
          visitor came from. */}
      <RunnerCard runner={runner} defaultExpanded onDeleted={() => navigate(backTo)} />
    </div>
  );
}
