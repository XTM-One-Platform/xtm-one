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
 * "Open the case" (#2370): the primary way from a work item filed on a CTEM
 * case (a review, validation, remediation, launch, mobilize or re-test gate)
 * to the case page, in the app. The backend derives the case from the gate key
 * and sends it only when the reader may read it (`item.ctem_case`), so the
 * button never leads to a 404. The case page's Back returns to the item.
 */
import { Icon } from "@filigran/design-system";
import { Link, useLocation } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import type { WorkItem, WorkItemCtemCase } from "@/types";

/** The ask's links without the ones that open *kase* (the agent linked it too). Pure. */
export function linksWithoutCase<T extends { url: string }>(links: readonly T[], kase: WorkItemCtemCase | null | undefined): T[] {
  if (!kase) return [...links];
  return links.filter((link) => !link.url.includes(`/ctem/cases/${kase.id}`));
}

export function WorkItemCaseLink({ item }: { item: WorkItem }) {
  const { t_i18n } = useFormatter();
  const location = useLocation();
  const kase = item.ctem_case;
  if (!kase) return null;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1" data-testid="work-item-case">
      <Button asChild className="gap-1.5">
        <Link
          to={kase.path}
          state={{ backTo: `${location.pathname}${location.search}`, backLabel: "Back to the work item" }}
          data-testid="work-item-case-link"
        >
          <Icon name="briefcase" size={16} className="shrink-0" aria-hidden />
          {t_i18n("Open the case")}
        </Link>
      </Button>
      {kase.title && (
        <span className="content-compact min-w-0 truncate text-default-secondary" title={kase.title} data-testid="work-item-case-title">
          {kase.title}
        </span>
      )}
    </div>
  );
}
