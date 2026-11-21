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
 * The home dashboard's "Top value drivers" card (#12009), the right half of the
 * Value row: the agents of the Value card's own report (`useValueReport`, the
 * same scope, window and cache entry, so the two cards never differ) ranked by
 * hours saved. Each row: the agent's icon tile (the Agents widget's, shared),
 * its name, a share bar against the first row, the hours saved and the runs.
 * The rows are as many as the card fits (`listCapacity`, 4 at the default
 * height), top-aligned, never clipped; under 480 px of card width the runs go,
 * under 360 px the bar goes (container queries), name and hours stay. A row
 * opens its agent; an agent the report names but that no longer exists is
 * drawn unlinked and named "Deleted agent". It starts no run and calls no
 * model. Community Edition: the same line as the Value card.
 */
import { VALUE_METHODOLOGY_PATH, valuePath } from "@/lib/task-routes";
import type { ComponentType } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { AgentIconTile } from "@/components/ui/agent-icon-tile";
import { CompactFigure } from "@/components/ui/compact-figure";
import { EmptyState } from "@/components/ui/empty-state";
import { TruncatedText } from "@/components/ui/truncated-text";
import { listCapacity } from "@/components/dashboard-grid/widget-density";
import { useWidgetSize } from "@/components/dashboard-grid/widget-size-context";
import { useValueReport } from "@/hooks/api/useRoiReportApi";
import { useDashboardStats } from "@/hooks/api/useMiscApi";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { useAuth } from "@/hooks/useAuth";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { VALUE_SCOPE_LINES, valueDrivers } from "@/lib/roi-value";
import { cn } from "@/lib/utils";

/** Header (title and scope lines), padding and the gap under the header, in px. */
const CHROME = 84;
/** One row, its hover band included. */
const ROW_HEIGHT = 32;
const MAX_ROWS = 5;

const EmptyGlyph: ComponentType<{ className?: string }> = ({ className }) => <Icon name="award" size={24} className={className} />;

export function RoiValueDriversCard() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const locked = useEnterpriseLockedReason();
  const scope = isAdmin ? "organization" : "mine";
  const report = useValueReport(scope === "organization" ? "platform" : "mine", "30d", { enabled: !locked });
  // The agents' icons: the dashboard's own stats, already cached by the Agents widget.
  const { data: stats } = useDashboardStats();
  const nf = useNumberFormat(report.data?.currency);
  const capacity = listCapacity(useWidgetSize(), { chrome: CHROME, rowHeight: ROW_HEIGHT, max: MAX_ROWS });
  const icons = new Map((stats?.agent_summaries ?? []).map((agent) => [agent.id, agent.icon]));
  const rows = report.data ? valueDrivers(report.data.agents).slice(0, capacity) : [];
  const top = rows[0]?.hours_saved ?? 0;

  return (
    <Card className="flex h-full flex-col" data-testid="home-value-drivers-card">
      <CardContent className="@container flex h-full flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex min-w-0 items-center gap-2 content-base font-semibold">
              <Icon name="award" size={16} className="shrink-0 text-accent" />
              <TruncatedText text={t_i18n("Top value drivers")} />
            </h2>
            <p className="content-compact text-default-secondary" data-testid="home-value-drivers-scope">
              {t_i18n(VALUE_SCOPE_LINES[scope])} · {t_i18n("Last 30 days")}
            </p>
          </div>
          {/* The By agent tab of the Value page (UX design round C3). */}
          <Button asChild variant="ghost" size="sm" className="h-7 shrink-0 content-compact gap-1" data-testid="home-value-drivers-all">
            <Link to={valuePath("agents")}>
              <Icon name="arrow-up-right" size={16} /> {t_i18n("All agents")}
            </Link>
          </Button>
        </div>
        {locked ? (
          <p className="content-compact text-default-secondary">{t_i18n("Value is part of Enterprise Edition.")}</p>
        ) : report.isError ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
            {t_i18n("The value could not be read.")}
            <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
              {t_i18n("Retry")}
            </Button>
          </div>
        ) : !report.data ? (
          <div className="flex flex-col" aria-busy="true">
            {Array.from({ length: Math.min(capacity, 4) }, (_, index) => (
              <div key={index} className="flex h-8 items-center">
                <div className="h-5 w-full rounded-sm bg-elevation-hover" />
              </div>
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState variant="inset" density="compact" icon={EmptyGlyph} title="No value recorded yet">
            <Button asChild variant="secondary" size="sm">
              <Link to={VALUE_METHODOLOGY_PATH}>{t_i18n("Declare a unit of work")}</Link>
            </Button>
          </EmptyState>
        ) : (
          <ol className="flex flex-col" data-testid="home-value-drivers-list">
            {rows.map((agent) => {
              const exists = Boolean(agent.id) && agent.name !== "";
              const name = exists ? agent.name : t_i18n("Deleted agent");
              const content = (
                <>
                  <AgentIconTile icon={exists ? icons.get(agent.id) : null} size="sm" muted={!exists} />
                  <span className="min-w-0 flex-1 content-compact">
                    <TruncatedText text={name} className={exists ? undefined : "text-default-secondary"} />
                  </span>
                  {/* The hours carry the value; the bar only shows the share against the first row. */}
                  <span aria-hidden className="h-1.5 w-1/4 min-w-16 max-w-40 shrink-0 rounded-full bg-elevation-hover @max-[360px]:hidden">
                    <span className="block h-full rounded-full bg-accent" style={{ width: `${top > 0 ? (agent.hours_saved / top) * 100 : 0}%` }} />
                  </span>
                  <CompactFigure
                    exact={t_i18n("{hours} h", { hours: nf.exactCount(agent.hours_saved) })}
                    className="w-16 shrink-0 text-right content-compact font-semibold"
                  >
                    {t_i18n("{hours} h", { hours: nf.count(agent.hours_saved) })}
                  </CompactFigure>
                  <CompactFigure
                    exact={t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count: agent.runs, shown: nf.exactCount(agent.runs) })}
                    className="w-24 shrink-0 text-right content-caption text-default-secondary @max-[480px]:hidden"
                  >
                    {t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count: agent.runs, shown: nf.count(agent.runs) })}
                  </CompactFigure>
                </>
              );
              const row = "flex h-8 items-center gap-2 rounded-sm px-1";
              return (
                <li key={agent.id || "deleted"}>
                  {exists ? (
                    <Link to={`/agents/${agent.id}`} className={cn(row, "transition-colors hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING)} data-testid="home-value-driver">
                      {content}
                    </Link>
                  ) : (
                    <div className={row} data-testid="home-value-driver-deleted">
                      {content}
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
