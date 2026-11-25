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
 * The fleet page's About view (#9174 slice 6b): what the fleet is for and how
 * it works, from its documentation (`GET /fleets/{id}/documentation`), always
 * reachable after setup, built-in or custom, in every mode.
 *
 * The documentation is always AI generated, never typed by hand (#9768 S2):
 * a reader who manages the fleet (built-in or custom) has one "Regenerate
 * documentation" button that writes the next version with the generator; a
 * built-in fleet regenerated locally can "Restore shipped version", and every
 * documented fleet has a "Version history" whose kept versions can be restored
 * (`FleetDocumentationHistoryDialog`). Documentation
 * older than the fleet's structure or member settings says so, with the same
 * Regenerate button; nothing regenerates on view. Reading it starts no run.
 *
 * The reader is the fleet's showcase (`FleetAboutShowcase`, #9539): hero with
 * the true status and figures, section index, structured sections. The
 * fleet's flow is drawn inline as the first section (slice S3, `flow`): the
 * page's own AgentFlowGraph, left to right, on the Graph view's data; the
 * "Open the graph" action goes to the Graph view for editing and forces.
 *
 * The KPI row's Value tile carries the fleet's value over 30 days (#9836 R6c,
 * #10123): the shared `RoiFigure` on the fleet's own report (`useFleetRoiReport`,
 * the ROI view's cache entry, read by the showcase), so both screens show one
 * number. No read on Community Edition or when the report is refused: the tile
 * shows its empty state, never an error. Reading it starts no run and calls no model.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Spinner } from "@filigran/design-system";
import { BookOpen, FileDown, History, RotateCcw, Sparkles, Workflow } from "lucide-react";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { DashboardNotice } from "@/components/ui/dashboard-notice";
import { EmptyState } from "@/components/ui/empty-state";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { PageSkeleton } from "@/components/ui/skeleton";
import {
  useExportFleetDatasheet,
  useFleetDocumentation,
  useFollowFleetDocGeneration,
  useFleetHealth,
  useRegenerateFleetDocumentation,
  useRestoreShippedFleetDocumentation,
  type FleetDocumentation,
  type FleetHealth,
  type FleetHealthStarts,
} from "@/hooks/api/useFleetApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import type { FleetView } from "@/lib/fleet-operations";
import { timeAgo } from "@/lib/utils";

import { FleetAboutFlow, FleetAboutShowcase } from "./FleetAboutShowcase";
import { FleetDocumentationHistoryDialog } from "./FleetDocumentationHistoryDialog";
import { authorKind, exportRefusal, regenerateRefusal, restoredLabel, writtenSections } from "./fleet-documentation";

export function FleetAboutView({
  fleetId,
  onOpenGraph,
  onOpenMembers,
  onOpenView,
  orchestratorId,
  flow,
  readOnly = false,
  healthStarts,
}: {
  fleetId: string;
  /** The Platform Health fleet only (`Fleet.health_starts`): its starts line on the About page (#2194). */
  healthStarts?: FleetHealthStarts | null;
  onOpenGraph: () => void;
  /** A fleet nobody may write (Platform Health, `read_only_reason`): no setup or fix action. */
  readOnly?: boolean;
  /** Opens another tab of the fleet: the KPI tiles' links (#10123). */
  onOpenView?: (view: FleetView) => void;
  /** The fleet's orchestrator: its member card comes first. */
  orchestratorId?: string | null;
  /** The fleet's flow diagram, drawn inline by the showcase (see FleetAboutShowcase). */
  flow?: ReactNode;
  /** The members view, where the members' automations are switched on. */
  onOpenMembers: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: doc, isLoading, isError } = useFleetDocumentation(fleetId);
  const { data: health } = useFleetHealth(fleetId);

  if (isLoading) return <PageSkeleton cards={2} />;
  if (isError || !doc) {
    return (
      <EmptyState
        icon={BookOpen}
        title={t_i18n("The documentation could not be read")}
        description={t_i18n("Try again in a moment.")}
      />
    );
  }
  return (
    <FleetDocumentationReader
      fleetId={fleetId}
      doc={doc}
      health={health}
      onOpenGraph={onOpenGraph}
      onOpenMembers={onOpenMembers}
      onOpenView={onOpenView}
      orchestratorId={orchestratorId}
      flow={flow}
      readOnly={readOnly}
      healthStarts={healthStarts}
    />
  );
}

function FleetDocumentationReader({
  fleetId,
  doc,
  health,
  onOpenGraph,
  onOpenMembers,
  onOpenView,
  orchestratorId,
  flow,
  readOnly,
  healthStarts,
}: {
  fleetId: string;
  doc: FleetDocumentation;
  health: FleetHealth | undefined;
  healthStarts?: FleetHealthStarts | null;
  readOnly: boolean;
  onOpenGraph: () => void;
  onOpenMembers: () => void;
  onOpenView?: (view: FleetView) => void;
  orchestratorId?: string | null;
  flow?: ReactNode;
}) {
  const { t_i18n, locale } = useFormatter();
  const sections = useMemo(() => writtenSections(doc), [doc]);
  const author = authorKind(doc.updated_by);
  const regenerate = useRegenerateFleetDocumentation(fleetId);
  const restore = useRestoreShippedFleetDocumentation(fleetId);
  // The one-page datasheet: rendered on the server, no model call, so it
  // works with AI switched off (#9768 S4).
  const datasheet = useExportFleetDatasheet(fleetId);
  const aiOff = useAiActionDisabled();
  // The running state is the server's (#10817): a generation started from this
  // page, another tab, the fleet tool or the fleet's creation spins here on
  // every mount, and the page follows it until the new version lands.
  const generating = regenerate.isPending || !!doc.generating;
  useFollowFleetDocGeneration(fleetId, !!doc.generating);
  const busy = generating || restore.isPending;
  const [historyOpen, setHistoryOpen] = useState(false);
  // Keyboard focus never falls to the page (UX round 1 B2): a pending action
  // is aria-disabled, not disabled, so the focused button keeps the focus, and
  // the Restore shipped button, gone once it worked, hands it to Regenerate.
  const regenerateRef = useRef<HTMLButtonElement>(null);
  // Set by a write the person started: once the version it saved is shown,
  // Regenerate gets the focus, which may be a new button (the first generation
  // swaps the empty-state layout for the documented one, UX round 2 B2).
  const focusRegenerate = useRef(false);
  useEffect(() => {
    if (!focusRegenerate.current) return;
    focusRegenerate.current = false;
    regenerateRef.current?.focus();
  }, [doc.version]);
  const keepFocusUnlessRefused = { onError: () => { focusRegenerate.current = false; } };

  // One model call on the person's click, counted like a chat; the answer is
  // saved as the next version (the previous one stays in the history).
  const onRegenerate = () => {
    if (aiOff || busy) return;
    focusRegenerate.current = document.activeElement === regenerateRef.current;
    regenerate.mutate({ base_version: doc.version }, keepFocusUnlessRefused);
  };

  const onRestoreShipped = () => {
    if (busy) return;
    focusRegenerate.current = true;
    restore.mutate(undefined, keepFocusUnlessRefused);
  };

  // The single Regenerate control of the tab, in the page-title actions.
  const regenerateButton = doc.editable && (
    <AiDisabledTip>
      <Button
        ref={regenerateRef}
        type="button"
        size="sm"
        className="h-8 gap-1.5"
        disabled={aiOff}
        aria-disabled={busy || undefined}
        aria-busy={generating || undefined}
        onClick={onRegenerate}
        data-testid="about-regenerate"
      >
        {generating && <Spinner size="sm" tone="inherit" />}
        {!generating && <Sparkles className="h-4 w-4" aria-hidden />}
        {generating
          ? t_i18n("Generating the documentation")
          : doc.version === 0
            ? t_i18n("Generate documentation")
            : t_i18n("Regenerate documentation")}
      </Button>
    </AiDisabledTip>
  );

  // Page-title actions: secondary first, the one filled primary at the right end.
  // One way to the Graph tab: the Flow section's header link when the flow is
  // drawn (#9757), the Open the graph button only when it is not.
  const openGraph = !flow && (
    <Button type="button" variant="outline" size="sm" className="h-8 gap-1.5" onClick={onOpenGraph}>
      <Workflow className="h-4 w-4" aria-hidden />
      {t_i18n("Open the graph")}
    </Button>
  );
  const actions = (
    <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
      {openGraph}
      {doc.version > 0 && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 gap-1.5"
          onClick={() => setHistoryOpen(true)}
          data-testid="about-history"
        >
          <History className="h-4 w-4" aria-hidden />
          {t_i18n("Version history")}
        </Button>
      )}
      {doc.version > 0 && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 gap-1.5"
          aria-disabled={datasheet.isPending || undefined}
          aria-busy={datasheet.isPending || undefined}
          onClick={() => { if (!datasheet.isPending) datasheet.mutate(); }}
          data-testid="about-export-pdf"
        >
          {datasheet.isPending && <Spinner size="sm" tone="inherit" />}
          {!datasheet.isPending && <FileDown className="h-4 w-4" aria-hidden />}
          {t_i18n("Export PDF")}
        </Button>
      )}
      {doc.editable && doc.local && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 gap-1.5"
          aria-disabled={busy || undefined}
          onClick={onRestoreShipped}
          data-testid="about-restore-shipped"
        >
          <RotateCcw className="h-4 w-4" aria-hidden />
          {t_i18n("Restore shipped version")}
        </Button>
      )}
      {regenerateButton}
      <FleetDocumentationHistoryDialog
        fleetId={fleetId}
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        canRestore={doc.editable}
        currentVersion={doc.version}
        onRegenerate={doc.editable && !aiOff ? onRegenerate : undefined}
      />
    </div>
  );

  // A generation that failed on the server reads its sentence on every mount
  // for a few minutes (#10817), the clicker's own refusal first.
  const refusal = regenerate.isError ? (
    <RefusalAlert message={regenerateRefusal(regenerate.error, t_i18n)} detail={regenerate.error?.message} />
  ) : doc.generation_failed && !generating ? (
    <div data-testid="about-generation-failed">
      <RefusalAlert message={regenerateRefusal(null, t_i18n)} detail={doc.generation_failed} />
    </div>
  ) : restore.isError ? (
    <RefusalAlert message={t_i18n("The shipped documentation could not be restored. Try again in a moment.")} detail={restore.error?.message} />
  ) : null;
  // A failed export reads its reason and what to do on its own line under the
  // actions row, at the 12 px body size (UX round 1 B1 on #9863).
  const exportError = datasheet.isError ? (
    <div data-testid="about-export-error">
      <RefusalAlert message={exportRefusal(datasheet.error, t_i18n)} detail={datasheet.error?.message} />
    </div>
  ) : null;

  if (sections.length === 0) {
    // No version yet (#10316): one panel says what happens next (a generation
    // running, or what Generate does), and the facts computed from the
    // configuration are drawn under it with the showcase's own blocks, so the
    // page is never empty and never a hand editor. Without computed facts the
    // flow alone stays under the panel. The panel is an info box only: the
    // header button is the single Generate action of the tab (Sam, 10 Nov 2026, #10817).
    const panel = generating ? (
      <DashboardNotice variant="progress" spin title={t_i18n("Generating the documentation")}>
        {t_i18n("It keeps running if you leave this page. The documentation shows here as soon as it is ready.")}
      </DashboardNotice>
    ) : (
      <DashboardNotice variant="info" icon={BookOpen} title={t_i18n("No documentation yet")}>
        {readOnly
          ? t_i18n("Platform Health is built in and read-only.")
          : doc.editable
          ? t_i18n("Generate documentation, at the top right, has the AI read the fleet's members, settings, automations and handovers and write its documentation. One generation, counted like a chat message.")
          : t_i18n("The owner of this fleet has not generated its documentation yet.")}
      </DashboardNotice>
    );
    return (
      <div className="space-y-4" data-testid="fleet-about-undocumented">
        {(openGraph || regenerateButton) && (
          <div className="flex flex-wrap items-center justify-end gap-2">
            {openGraph}
            {regenerateButton}
          </div>
        )}
        <div data-testid={generating ? "about-generating" : "about-generate-panel"}>{panel}</div>
        {refusal}
        {doc.structured ? (
          <FleetAboutShowcase
            fleetId={fleetId}
            doc={doc}
            health={health}
            healthStarts={healthStarts}
            orchestratorId={orchestratorId}
            onManageAutomations={onOpenMembers}
            onOpenView={onOpenView}
            flow={flow}
            onOpenGraph={onOpenGraph}
            readOnly={readOnly}
          />
        ) : (
          flow && <FleetAboutFlow flow={flow} onOpenGraph={onOpenGraph} />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="fleet-about">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="content-compact text-default-secondary" data-testid="about-author">
          {doc.restored_from
            ? doc.updated_at
              ? t_i18n("{label}, version {version}, {when}", {
                label: restoredLabel(doc.restored_from, t_i18n),
                version: doc.version,
                when: timeAgo(doc.updated_at, locale),
              })
              : restoredLabel(doc.restored_from, t_i18n)
            : author === "seed"
            ? t_i18n("Written by Filigran, shipped with the platform")
            : author === "fork"
              ? t_i18n("Copied from the original fleet when it was duplicated")
              : doc.updated_at
                ? t_i18n("Generated by AI, version {version}, {when}", { version: doc.version, when: timeAgo(doc.updated_at, locale) })
                : t_i18n("Version {version}", { version: doc.version })}
        </p>
        {actions}
      </div>
      {exportError}
      {refusal}
      {/* An info box only: the header button is the single Regenerate action (Sam, 10 Nov 2026, #10817). */}
      {doc.out_of_date === true && (
        <div data-testid="about-out-of-date">
          <DashboardNotice variant="info" title={t_i18n("The fleet changed since this documentation was generated")}>
            {doc.editable ? t_i18n("Regenerate documentation, at the top right, brings it up to date.") : null}
          </DashboardNotice>
        </div>
      )}
      <FleetAboutShowcase
        fleetId={fleetId}
        doc={doc}
        health={health}
        healthStarts={healthStarts}
        orchestratorId={orchestratorId}
        onManageAutomations={onOpenMembers}
        onOpenView={onOpenView}
        flow={flow}
        onOpenGraph={onOpenGraph}
        readOnly={readOnly}
      />
    </div>
  );
}
