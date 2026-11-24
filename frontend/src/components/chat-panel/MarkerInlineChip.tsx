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
 * The compact form of a deck, report, video, dashboard, investigation or
 * scheduled work item marker the answer carries inside a table cell, a list
 * item or a quote (@filigran/chatbot `ChatMarkerContext.inline`, #11697): a
 * chip with the kind's icon, its kind, the title cut to fit and a link that
 * opens it. The full card is for a marker on its own line.
 */
import { workItemPath } from "@/lib/task-routes";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import type { IconName } from "@filigran/design-system";
import { useCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useInvestigation } from "@/hooks/api/useInvestigationApi";
import { usePresentation } from "@/hooks/api/usePresentationApi";
import { useReport } from "@/hooks/api/useReportApi";
import { useVideoProject } from "@/hooks/api/useVideoStudioApi";
import { useWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import { inlineChipClass } from "./inline-chip";

/** The kinds `MarkerInlineChip` draws. */
export type InlineMarkerKind = "PRESENTATION" | "REPORT" | "VIDEO" | "DASHBOARD" | "INVESTIGATION" | "WORK_ITEM";

const CHIP_CLASS = inlineChipClass();

interface ChipViewProps {
  icon: IconName;
  kindLabel: string;
  title: string | null | undefined;
  href: string | null;
  loading: boolean;
}

function ChipView({ icon, kindLabel, title, href, loading }: ChipViewProps): ReactNode {
  const { t_i18n } = useFormatter();
  const glyph = <Icon name={icon} size={16} className="shrink-0 text-accent" aria-hidden />;
  if (loading) {
    return (
      <span className={CHIP_CLASS} data-testid="marker-inline-chip" aria-busy="true">
        {glyph}
        <span className="text-default-secondary">{kindLabel}</span>
        <span className="h-3 w-20 animate-pulse rounded-sm bg-elevation-hover" aria-label={t_i18n("Loading")} />
      </span>
    );
  }
  if (!title) {
    return (
      <span className={CHIP_CLASS} data-testid="marker-inline-chip">
        {glyph}
        <span className="text-default-secondary">{kindLabel}</span>
        <span className="truncate">{t_i18n("Unavailable")}</span>
      </span>
    );
  }
  if (!href) {
    return (
      <span className={CHIP_CLASS} title={title} data-testid="marker-inline-chip">
        {glyph}
        <span className="truncate font-medium text-default-primary">{title}</span>
        <span className="shrink-0 text-default-secondary">{kindLabel}</span>
      </span>
    );
  }
  return (
    <Link
      to={href}
      title={title}
      className={inlineChipClass(true)}
      data-testid="marker-inline-chip"
    >
      {glyph}
      <span className="truncate font-medium text-default-primary">{title}</span>
      <span className="shrink-0 text-default-secondary">{kindLabel}</span>
    </Link>
  );
}

function DeckChip({ id, slideRef }: { id: string; slideRef: string | null }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = usePresentation(id);
  const href = data ? `/presentations/${data.id}${slideRef ? `#/${slideRef}` : ""}` : null;
  return <ChipView icon="presentation" kindLabel={t_i18n("Deck")} title={data?.title} href={href} loading={isLoading} />;
}

function ReportChip({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useReport(id);
  return (
    <ChipView icon="file-text" kindLabel={t_i18n("Report")} title={data?.title} href={data ? `/reports/${data.id}` : null} loading={isLoading} />
  );
}

function VideoChip({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useVideoProject(id);
  return (
    <ChipView icon="clapperboard" kindLabel={t_i18n("Video")} title={data?.title} href={data ? `/videos/${data.id}` : null} loading={isLoading} />
  );
}

function DashboardChip({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useCustomDashboard(id);
  return (
    <ChipView
      icon="layout-dashboard"
      kindLabel={t_i18n("Dashboard")}
      title={data?.title}
      href={data ? `/dashboards/${data.id}` : null}
      loading={isLoading}
    />
  );
}

function InvestigationChip({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useInvestigation(id);
  // An investigation lives in its conversation and has no page of its own:
  // the chip names its subject, with no link.
  return (
    <ChipView icon="search" kindLabel={t_i18n("Investigation")} title={data?.subject_value} href={null} loading={isLoading} />
  );
}

function WorkItemChip({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useWorkItem(id);
  return (
    <ChipView
      icon="calendar"
      kindLabel={t_i18n("Work item")}
      title={data?.title}
      href={data ? workItemPath(data.id) : null}
      loading={isLoading}
    />
  );
}

/** The compact chip of a *kind* marker naming *id* (a deck's slide as *slideRef*). */
export function MarkerInlineChip({ kind, id, slideRef = null }: { kind: InlineMarkerKind; id: string; slideRef?: string | null }) {
  switch (kind) {
    case "PRESENTATION":
      return <DeckChip id={id} slideRef={slideRef} />;
    case "REPORT":
      return <ReportChip id={id} />;
    case "VIDEO":
      return <VideoChip id={id} />;
    case "DASHBOARD":
      return <DashboardChip id={id} />;
    case "INVESTIGATION":
      return <InvestigationChip id={id} />;
    case "WORK_ITEM":
      return <WorkItemChip id={id} />;
  }
}
