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
 * Every published dashboard link, for administrators to review (as OpenCTI's
 * list of public dashboards): whose dashboard, who published it, how fresh
 * its snapshot is - and a way to turn any of them off at once. The links
 * themselves are not shown: a manager copies one from its dashboard.
 */
import { Link } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { Globe } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { useDashboardPublicLinks, useTurnOffDashboardPublicLink } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";

export function PublicLinksDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const links = useDashboardPublicLinks(open);
  const turnOff = useTurnOffDashboardPublicLink();
  const rows = links.data ?? [];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90dvh] max-w-3xl flex-col gap-4 overflow-y-auto" data-testid="dashboards.public-links">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Globe className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Public links")}
          </DialogTitle>
          <DialogDescription>{t_i18n("Every dashboard anyone holding its address can see without signing in, with the data of the person who published it.")}</DialogDescription>
        </DialogHeader>
        {links.isPending ? (
          <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
        ) : rows.length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No dashboard is published.")}</p>
        ) : (
          <ul className="flex flex-col">
            {rows.map((link) => (
              <li key={link.dashboard_id} className="flex items-center gap-3 border-b border-elevation-subtle-soft py-2 last:border-b-0" data-testid="dashboards.public-links.row">
                <div className="flex min-w-0 flex-1 flex-col">
                  <Link to={`/dashboards/${link.dashboard_id}`} className="truncate content-base font-medium text-default-primary hover:underline">
                    {link.dashboard_title}
                  </Link>
                  <span className="content-caption text-default-secondary">
                    {[
                      link.publisher_name ? t_i18n("Published by {name}", { name: link.publisher_name }) : null,
                      link.snapshot_at ? t_i18n("Data read {when}", { when: timeAgo(link.snapshot_at, locale) }) : null,
                      t_i18n("{count, plural, one {Read again every # minute} other {Read again every # minutes}}", { count: link.refresh_minutes }),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={turnOff.isPending}
                  onClick={() =>
                    turnOff.mutate(link.dashboard_id, {
                      onSuccess: () => void links.refetch(),
                      onError: (error) => toast({ title: t_i18n("The link could not be turned off"), description: error.message, variant: "error" }),
                    })
                  }
                >
                  {t_i18n("Turn off")}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
