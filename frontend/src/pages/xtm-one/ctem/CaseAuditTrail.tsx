// SPDX-License-Identifier: Apache-2.0

/**
 * The case History as an audit trail (#1490, slice 3; v2 #1679, slices 3 and 4):
 * a phase band (the CTEM phases in order, the current one marked, the time spent
 * in each; a press filters the views), a Table view (default: one line per row,
 * sort, search, filters, CSV export) and a Timeline view (one continuous rail
 * grouped by phase, a node icon per event kind, one line per entry). A row of
 * either view opens the same event dialog with its full details: who acted,
 * what was done, the change, a note's whole text, a gate's decision record, a
 * run's link. The words of each event come from the page (`renderEvent`), so
 * this component holds the structure only.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useFormatter } from "@/i18n";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCasePhase, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import {
  ACTOR_TYPE_LABEL,
  AUDIT_PHASE_LABEL,
  AUTONOMY_CLASSES,
  AUTONOMY_LABEL,
  COUNTED_AUTONOMY_CLASSES,
  filterRows,
  foldAnsweredRequests,
  groupByPhase,
  isAuditPhase,
  phaseBand,
  type AuditPhase,
  type AuditRow,
} from "@/lib/ctem-case-audit";
import { formatCount } from "@/lib/number-format";
import { formatDurationShort, cn } from "@/lib/utils";
import { CaseEventDialog } from "./CaseEventDialog";
import { CASE_SECTION_ICON, CaseAutonomyChip, CaseEmptyLine, CaseIcon, actorIcon, caseEventIcon } from "./case-icons";

type HistoryView = "timeline" | "table";

/** The two views of the History, the Table first (default, #1679). @i18n-keys */
const VIEW_LABEL = {
  table: "Table",
  timeline: "Timeline",
} as const satisfies Record<HistoryView, string>;

interface CaseAuditTrailProps {
  rows: AuditRow[];
  phases: CtemCasePhase[] | undefined;
  currentPhase: string | null | undefined;
  /** The words of one event: its action and its from -> to, already translated. */
  renderEvent: (event: CtemCaseEvent) => ReactNode;
  /** The words of one fleet run: its line and its outcome badge, linked to the run. */
  renderRun: (run: CtemCaseRun) => ReactNode;
  /** The name of whoever acted on an event (person, agent or system), already translated. */
  actorName: (row: AuditRow) => string;
  /** The text the CSV and the search read for an event's action (plain words). */
  actionText: (row: AuditRow) => string;
  /**
   * An event's from -> to in words (the labels its action uses, the owner by name, the due
   * date as a date; an ISO date for the CSV), null when it changed nothing it can say.
   */
  changeText: (event: CtemCaseEvent, forCsv?: boolean) => { from: string; to: string } | null;
  /** A person's name from their user id (a gate answered by a person names them by id). */
  personName: (id: string | null | undefined) => string | null;
  /** The case's resolved gate records: the dialog of a gate row shows its decision record. */
  gates?: readonly CtemCaseGateRecord[];
  /** The case history: who answered a gate when its record cannot name them. */
  history?: readonly CtemCaseEvent[];
  /** Where a run page opened from the dialog goes back to. */
  backTo: string;
}

export function CaseAuditTrail({
  rows,
  phases,
  currentPhase,
  renderEvent,
  renderRun,
  actorName,
  actionText,
  changeText,
  personName,
  gates,
  history,
  backTo,
}: CaseAuditTrailProps) {
  const { t_i18n, intl } = useFormatter();
  const [view, setView] = useState<HistoryView>("table");
  const [opened, setOpened] = useState<AuditRow | null>(null);
  const [phase, setPhase] = useState<AuditPhase | null>(null);
  const [collapsed, setCollapsed] = useState<Set<AuditPhase>>(new Set());
  const band = useMemo(() => phaseBand(phases), [phases]);
  const shown = useMemo(() => filterRows(rows, { phase }), [rows, phase]);
  // The Timeline folds a gate request into its answer: one entry per gate (#1679, point 2).
  const groups = useMemo(() => groupByPhase(foldAnsweredRequests(shown)), [shown]);
  const current = isAuditPhase(currentPhase) ? currentPhase : null;

  const date = (at: string | null) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : "");

  const autonomyBadge = (row: AuditRow) => <CaseAutonomyChip autonomy={row.autonomy} testId="ctem-audit-autonomy" />;

  // The actor's type is said only when it adds to the name shown (UX round 1 on #1520, B5:
  // never "System System" or "Agent Agent").
  const actorType = (row: AuditRow): string | null => {
    const type = t_i18n(ACTOR_TYPE_LABEL[row.actorType]);
    return type.trim().toLowerCase() === actorName(row).trim().toLowerCase() ? null : type;
  };
  // One line on both views (#1679): the actor's type is its icon (a person, an agent, the
  // system), said in words to assistive technology only when it adds to the name.
  // On a Timeline line the actor stays whole (up to 12rem, then truncated) and the action
  // beside it takes the cut; in the table it fills its column (UX round 2 on #1708, B5).
  const actorCell = (row: AuditRow, onLine = false) => {
    const type = actorType(row);
    const width = onLine ? "shrink-0 max-w-48" : "min-w-0 max-w-full";
    return (
      // `flex`, as the Action cell: an inline-flex box sat on the text baseline, 2 px high (UX round 1 on #1708, B3).
      <span className={`flex ${width} items-center gap-1.5`} data-testid="ctem-audit-actor" data-actor-type={row.actorType}>
        <CaseIcon name={actorIcon(row.actorType)} className="text-default-secondary" />
        <TruncatedText text={actorName(row)} className="font-medium" />
        {type && <span className="sr-only">{type}</span>}
      </span>
    );
  };
  const rowIcon = (row: AuditRow) => caseEventIcon(row.kind === "run" ? "run" : row.event.kind);
  const change = (r: AuditRow, forCsv = false) => (r.kind === "event" && r.event.kind !== "gate" ? changeText(r.event, forCsv) : null);

  const columns: DataTableColumn<AuditRow>[] = [
    {
      key: "at",
      // Own key: the shared "Time" key reads as a duration in fr/es/ja (Durée).
      header: "Date and time",
      cell: (r) => <span className="whitespace-nowrap">{date(r.at)}</span>,
      sortValue: (r) => (r.at ? Date.parse(r.at) : 0),
      csv: (r) => r.at ?? "",
      firstSort: "desc",
    },
    {
      key: "phase",
      header: "Phase",
      // The band and the filter already say the phase: below 6xl the column goes so the
      // table fits a 1080 screen (UX round 2 on #1520, B3).
      hideable: true,
      hideBelow: "6xl",
      cell: (r) => t_i18n(AUDIT_PHASE_LABEL[r.phase]),
      sortValue: (r) => r.phase,
      csv: (r) => r.phase,
    },
    {
      key: "actor",
      header: "Actor",
      cell: (row) => actorCell(row),
      sortValue: (r) => actorName(r),
      csv: (r) => actorName(r),
    },
    {
      key: "action",
      header: "Action",
      // One line, cut with the shared tooltip, its kind's icon before it; the row opens the
      // event dialog with everything else (#1679). 16 rem below 6xl so the table fits at
      // 1080, 32 rem above (UX round 2 on #1520, B3).
      cell: (r) => (
        <span className="flex max-w-[16rem] items-center gap-1.5 @6xl:max-w-[32rem]">
          <CaseIcon name={rowIcon(r)} className="text-default-secondary" />
          <TruncatedText text={actionText(r)} />
        </span>
      ),
      csv: (r) => actionText(r),
    },
    {
      key: "autonomy",
      header: "Autonomy",
      cell: autonomyBadge,
      sortValue: (r) => AUTONOMY_CLASSES.indexOf(r.autonomy),
      csv: (r) => r.autonomy,
    },
    {
      key: "change",
      // Its own key (UX round 5 on #1520, B1): the shared "Change" is the verb in fr and es.
      header: "Change (from → to)",
      hideable: true,
      hideBelow: "6xl",
      cell: (r) => {
        const c = change(r);
        return c ? <span className="whitespace-nowrap">{`${c.from} → ${c.to}`}</span> : null;
      },
      csv: (r) => {
        const c = change(r, true);
        return c ? `${c.from} -> ${c.to}` : "";
      },
    },
    {
      key: "details",
      header: "Details",
      exportOnly: true,
      cell: () => null,
      csv: (r) => (r.kind === "event" ? (r.event.reason ?? "") : r.run.id),
    },
  ];

  return (
    <div className="space-y-4" data-testid="ctem-audit-trail">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <ol className="flex flex-wrap gap-2" aria-label={t_i18n("CTEM phases")} data-testid="ctem-audit-phases">
          {band.map((p) => {
            const key = p.phase as AuditPhase;
            const selected = phase === key;
            const reached = p.entered_at !== null;
            return (
              <li key={key}>
                <button
                  type="button"
                  aria-pressed={selected}
                  disabled={!reached}
                  onClick={() => setPhase(selected ? null : key)}
                  className={cn(
                    "flex min-w-[8rem] flex-col items-start rounded-sm border px-3 py-1.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                    selected ? "border-feedback-info-primary bg-elevation-highlight" : "border-elevation-subtle hover:bg-elevation-hover",
                    !reached && "opacity-60",
                  )}
                  data-testid={`ctem-audit-phase-${key}`}
                >
                  <span className="flex items-center gap-1.5 content-base font-medium">
                    {t_i18n(AUDIT_PHASE_LABEL[key])}
                    {p.phase === current && <Badge variant="default">{t_i18n("Current")}</Badge>}
                  </span>
                  <span className="content-compact text-default-secondary">
                    {reached ? formatDurationShort(p.seconds) : t_i18n("Not reached")}
                    {p.waiting > 0 && ` · ${t_i18n("{count, plural, =0 {no gate waiting} one {# gate waiting} other {# gates waiting}}", { count: p.waiting })}`}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
        <SegmentedFilter
          options={(["table", "timeline"] as const).map((key) => ({ key, label: VIEW_LABEL[key] }))}
          value={view}
          onChange={setView}
          ariaLabel={t_i18n("History view")}
          selection="radio"
          size="sm"
          testId="ctem-audit-view"
        />
      </div>

      {view === "table" ? (
        <DataTable
          framed={false}
          rows={shown}
          columns={columns}
          rowKey={(r) => r.key}
          defaultSort={{ key: "at", direction: "desc" }}
          defaultPageSize={25}
          searchFields={(r) => [actorName(r), actionText(r), r.kind === "event" ? r.event.reason : null]}
          searchPlaceholder={t_i18n("Search the history")}
          filtersInPill
          filters={[
            { key: "autonomy", label: "Autonomy", values: (r) => [r.autonomy], options: AUTONOMY_CLASSES.map((c) => ({ value: c, label: t_i18n(AUTONOMY_LABEL[c]) })) },
            {
              key: "actor_type",
              label: "Actor type",
              values: (r) => [r.actorType],
              options: (["person", "agent", "system"] as const).map((a) => ({ value: a, label: t_i18n(ACTOR_TYPE_LABEL[a]) })),
            },
          ]}
          exportName="ctem-case-audit-trail"
          urlKey="h_"
          emptyLabel={t_i18n("Nothing happened on this case yet")}
          emptyContent={
            <div className="flex justify-center py-10">
              <CaseEmptyLine icon={CASE_SECTION_ICON.history} testId="ctem-audit-empty">
                {t_i18n("Nothing happened on this case yet")}
              </CaseEmptyLine>
            </div>
          }
          rowTestId="ctem-audit-row"
          rowLines={1}
          onRowClick={setOpened}
        />
      ) : (
        <div className="space-y-4" data-testid="ctem-case-history">
          {groups.length === 0 && (
            <CaseEmptyLine icon={CASE_SECTION_ICON.history} testId="ctem-audit-empty">
              {t_i18n("Nothing happened on this case yet")}
            </CaseEmptyLine>
          )}
          {groups.map((group) => {
            const isCollapsed = collapsed.has(group.phase);
            const info = band.find((p) => p.phase === group.phase);
            return (
              <section key={group.phase} aria-labelledby={`ctem-audit-group-${group.phase}`} data-testid={`ctem-audit-group-${group.phase}`}>
                <h3 id={`ctem-audit-group-${group.phase}`} className="flex flex-wrap items-center gap-2 border-b border-elevation-subtle pb-1">
                  <button
                    type="button"
                    aria-expanded={!isCollapsed}
                    onClick={() =>
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        if (next.has(group.phase)) next.delete(group.phase);
                        else next.add(group.phase);
                        return next;
                      })
                    }
                    className="content-base font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                  >
                    {t_i18n(AUDIT_PHASE_LABEL[group.phase])}
                  </button>
                  {info && info.entered_at && (
                    <span className="content-compact text-default-secondary">{formatDurationShort(info.seconds)}</span>
                  )}
                  {COUNTED_AUTONOMY_CLASSES.filter((c) => group.counts[c] > 0).map((c) => (
                    <CaseAutonomyChip key={c} autonomy={c} count={formatCount(intl.locale, group.counts[c])} testId="ctem-audit-group-count" />
                  ))}
                </h3>
                {!isCollapsed && (
                  // One continuous rail (#1679, slice 4): a node with the event kind's icon per
                  // entry, one fixed-height line each (actor, action, autonomy, time); the whole
                  // line opens the event dialog, where the note body, the gate story and the
                  // decision record live.
                  <ol className="relative mt-2 before:absolute before:bottom-4 before:left-4 before:top-4 before:w-px before:bg-[var(--border-elevation-subtle)]">
                    {group.rows.map((row) => (
                      <li key={row.key} data-testid={row.kind === "run" ? "ctem-case-run" : "ctem-audit-event"}>
                        <button
                          type="button"
                          onClick={() => setOpened(row)}
                          aria-haspopup="dialog"
                          aria-label={t_i18n("Open the details of {action}", { action: actionText(row) })}
                          className="relative grid h-10 w-full grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-x-3 rounded-sm text-left content-base transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                          data-testid="ctem-audit-timeline-entry"
                        >
                          <span className="z-[1] flex h-8 w-8 items-center justify-center rounded-full border border-elevation-subtle layer-1 bg-elevation-default">
                            <CaseIcon name={rowIcon(row)} className="text-default-secondary" />
                          </span>
                          <span className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
                            {actorCell(row, true)}
                            <TruncatedText text={actionText(row)} className="text-default-secondary" />
                          </span>
                          <span className="flex shrink-0 items-center gap-2 whitespace-nowrap">
                            {autonomyBadge(row)}
                            {row.at && <span className="text-default-secondary">{date(row.at)}</span>}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            );
          })}
        </div>
      )}
      <CaseEventDialog
        row={opened}
        onClose={() => setOpened(null)}
        renderEvent={renderEvent}
        renderRun={renderRun}
        actorName={actorName}
        actionText={actionText}
        changeText={(e) => changeText(e)}
        personName={personName}
        gates={gates}
        history={history}
        backTo={backTo}
      />
    </div>
  );
}
