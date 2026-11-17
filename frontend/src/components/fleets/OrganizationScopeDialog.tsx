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
 * The organization scope editor of the Members view (dev-docs/ctem-fleets.md
 * 10.3): the platform's own organization is always in scope; the editor adds
 * every monitored organization, or a list of tracked ones. One dialog for the
 * fleet default, one member and a selection. Given a fleet, the editor shows
 * the run estimate of the picked scope before the save (10.5, A5).
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { Switch } from "@/components/ui/switch";
import { useFleetRunEstimate, type FleetOrganizationScope } from "@/hooks/api/useFleetApi";
import type { Organization } from "@/hooks/api/useOrganizationsApi";
import { useFormatter } from "@/i18n";

import {
  MEMBER_SETTING_LABELS,
  ORGANIZATION_SCOPE_WORDS,
  scopeEstimateIds,
  scopeEstimateSummary,
  scopeFromPicks,
  trackedOrganizations,
} from "./members-view";

export function OrganizationScopeDialog({
  open,
  onOpenChange,
  scope,
  organizations,
  description,
  fleetId,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The scope the editor starts from. */
  scope: FleetOrganizationScope;
  organizations: readonly Organization[];
  /** Who the change applies to, translated. */
  description: string;
  /** The fleet whose run estimate the editor shows; none (a preset) shows no estimate. */
  fleetId?: string;
  onApply: (scope: FleetOrganizationScope) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n(MEMBER_SETTING_LABELS.organization_scope)}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {/* Mounted per opening: the picks start from the scope given each time. */}
        {open && (
          <ScopeForm scope={scope} organizations={organizations} fleetId={fleetId} onCancel={() => onOpenChange(false)} onApply={onApply} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function ScopeForm({
  scope,
  organizations,
  fleetId,
  onCancel,
  onApply,
}: {
  scope: FleetOrganizationScope;
  organizations: readonly Organization[];
  fleetId?: string;
  onCancel: () => void;
  onApply: (scope: FleetOrganizationScope) => void;
}) {
  const { t_i18n } = useFormatter();
  const [monitored, setMonitored] = useState(scope.mode === "monitored");
  const [picked, setPicked] = useState<string[]>(scope.mode === "list" ? scope.organization_ids : []);
  const tracked = trackedOrganizations(organizations);
  const primary = organizations.find((o) => o.role === "primary");
  const options = tracked.map((o) => ({ value: o.id, label: o.name }));
  const primaryName = primary?.name || t_i18n(ORGANIZATION_SCOPE_WORDS.primary);

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        onApply(scopeFromPicks(monitored, picked));
      }}
    >
      <p className="content-compact text-default-secondary" data-testid="organization-scope-primary">
        {t_i18n("Always in scope: {name}", { name: primaryName })}
      </p>
      {/* Labels at body size with a help line each (Sam's standards 2 and 6): what "monitored" covers, and what no pick means. */}
      <div className="space-y-1">
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor="organization-scope-monitored">{t_i18n(ORGANIZATION_SCOPE_WORDS.monitored)}</Label>
          <Switch
            id="organization-scope-monitored"
            aria-label={t_i18n(ORGANIZATION_SCOPE_WORDS.monitored)}
            aria-describedby="organization-scope-monitored-help"
            checked={monitored}
            onCheckedChange={setMonitored}
          />
        </div>
        <p id="organization-scope-monitored-help" className="content-compact text-default-secondary">
          {t_i18n("Every tracked organization with monitoring on, now and later")}
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="organization-scope-list">{t_i18n("Tracked organizations")}</Label>
        <MultiSelect
          id="organization-scope-list"
          aria-label={t_i18n("Tracked organizations")}
          options={monitored ? [] : options}
          selected={monitored ? [] : picked}
          onToggle={(id) => setPicked((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]))}
          placeholder={monitored ? t_i18n("Every monitored organization is in scope") : t_i18n("Add tracked organizations")}
          searchPlaceholder={t_i18n("Search...")}
          emptyOptionsLabel={monitored ? t_i18n("Every monitored organization is in scope") : t_i18n("No tracked organization yet")}
          noMatchLabel={t_i18n("No match")}
          className="w-full"
        />
        <p className="content-compact text-default-secondary" data-testid="organization-scope-list-help">
          {t_i18n("No pick: only {name}", { name: primaryName })}
        </p>
      </div>
      {fleetId && <ScopeEstimateLine fleetId={fleetId} ids={scopeEstimateIds(monitored, picked, organizations)} />}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onCancel}>
          {t_i18n("Cancel")}
        </Button>
        <Button type="submit">{t_i18n("Apply")}</Button>
      </div>
    </form>
  );
}

/**
 * The run estimate of the picked tracked organizations, read before the save:
 * the expected runs per month this fleet's members add for them (the primary
 * is not counted). Polite live region, so the figure is heard as it changes.
 */
function ScopeEstimateLine({ fleetId, ids }: { fleetId: string; ids: string[] }) {
  const { t_i18n } = useFormatter();
  const estimate = useFleetRunEstimate(fleetId, ids);
  let text: string;
  if (ids.length === 0) text = t_i18n("No tracked organization in scope: no runs added");
  else if (estimate.isError) text = t_i18n("The run estimate could not be read");
  else if (!estimate.data) text = t_i18n("Estimating the runs...");
  else {
    const { runs, read, unread, unserved } = scopeEstimateSummary(estimate.data);
    if (unserved) text = t_i18n("No member trigger serves these organizations: no runs added");
    else if (unread > 0 && read === 0) {
      text = t_i18n("{count, plural, one {No run estimate: # organization not estimated (no OpenCTI count)} other {No run estimate: # organizations not estimated (no OpenCTI count)}}", { count: unread });
    } else text = t_i18n("{count, plural, one {About # run a month for this fleet} other {About # runs a month for this fleet}}", { count: runs });
    if (unread > 0 && read > 0) {
      text += ` ${t_i18n("{count, plural, one {(# organization not estimated: no OpenCTI count)} other {(# organizations not estimated: no OpenCTI count)}}", { count: unread })}`;
    }
  }
  return (
    <p className="content-compact text-default-secondary" aria-live="polite" data-testid="organization-scope-estimate">
      {text}
    </p>
  );
}
