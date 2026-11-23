// SPDX-License-Identifier: Apache-2.0

/**
 * The case History as an audit trail (#1490, slice 3): a phase band (the CTEM
 * phases in order, the current one marked, the time spent in each; a press
 * filters the views), a Timeline view grouped by phase (default) and a Table
 * view of the same rows (sort, search, filters, CSV export). Each row says who
 * acted, what was done, and whether it ran on its own, was validated by a
 * person or by policy, changed the case, or waits for a person. A note's long
 * body stays behind "Show more". The words of each event come from the page
 * (`renderEvent`), so this component holds the structure only.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import type { CtemCaseEvent, CtemCasePhase, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import {
  ACTOR_TYPE_LABEL,
  AUDIT_PHASE_LABEL,
  AUTONOMY_CLASSES,
  AUTONOMY_LABEL,
  AUTONOMY_VARIANT,
  filterRows,
  groupByPhase,
  isAuditPhase,
  phaseBand,
  type AuditPhase,
  type AuditRow,
} from "@/lib/ctem-case-audit";
import { formatCount } from "@/lib/number-format";
import { formatDurationShort, cn } from "@/lib/utils";

type HistoryView = "timeline" | "table";

/** The two views of the History. @i18n-keys */
const VIEW_LABEL = {
  timeline: "Timeline",
  table: "Table",
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
  /** A person's name from their user id (a gate answered by a person names them by id). */
  personName: (id: string | null | undefined) => string | null;
}

/** The long body of a note, shown under its headline on demand. */
function NoteBody({ event }: { event: CtemCaseEvent }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const body = (event.reason ?? "").trim();
  if (event.kind !== "note" || !body || body === (event.headline ?? "").trim()) return null;
  return (
    <div className="basis-full">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1 content-compact text-default-secondary"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        data-testid="ctem-audit-note-toggle"
      >
        {t_i18n(open ? "Show less" : "Show more")}
      </Button>
      {open && (
        <p className="mt-1 whitespace-pre-wrap content-compact text-default-secondary" data-testid="ctem-audit-note-body">
          {body}
        </p>
      )}
    </div>
  );
}

/** Since when a gate waited, or when and how it was answered: one line under the gate. */
function GateStory({ event, actorName }: { event: CtemCaseEvent; actorName: (id: string | null) => string }) {
  const { t_i18n, intl } = useFormatter();
  if (event.kind !== "gate") return null;
  const date = (at: string | null | undefined) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : "");
  if (event.to === "waited" && event.answer === null) {
    return (
      <span className="basis-full content-compact text-feedback-warning-primary" data-testid="ctem-audit-gate-story">
        {t_i18n("Still waiting since {date}", { date: date(event.at) })}
      </span>
    );
  }
  if (event.to === "waited" && event.answer) {
    return (
      <span className="basis-full content-compact text-default-secondary" data-testid="ctem-audit-gate-story">
        {t_i18n("Answered by {name} on {date}", { name: actorName(event.answer.by), date: date(event.answer.at) })}
      </span>
    );
  }
  if (event.waited_since) {
    return (
      <span className="basis-full content-compact text-default-secondary" data-testid="ctem-audit-gate-story">
        {t_i18n("Waited since {date}", { date: date(event.waited_since) })}
      </span>
    );
  }
  return null;
}

export function CaseAuditTrail({ rows, phases, currentPhase, renderEvent, renderRun, actorName, actionText, personName }: CaseAuditTrailProps) {
  const { t_i18n, intl } = useFormatter();
  const [view, setView] = useState<HistoryView>("timeline");
  const [phase, setPhase] = useState<AuditPhase | null>(null);
  const [collapsed, setCollapsed] = useState<Set<AuditPhase>>(new Set());
  const band = useMemo(() => phaseBand(phases), [phases]);
  const shown = useMemo(() => filterRows(rows, { phase }), [rows, phase]);
  const groups = useMemo(() => groupByPhase(shown), [shown]);
  const current = isAuditPhase(currentPhase) ? currentPhase : null;

  const date = (at: string | null) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : "");
  const personOf = (id: string | null) => personName(id) ?? t_i18n("a person");

  const autonomyBadge = (row: AuditRow) => (
    <Badge variant={AUTONOMY_VARIANT[row.autonomy]} className="shrink-0" data-testid="ctem-audit-autonomy">
      {t_i18n(AUTONOMY_LABEL[row.autonomy])}
    </Badge>
  );

  const actorChip = (row: AuditRow) => (
    <span className="inline-flex items-baseline gap-1">
      <span className="font-medium">{actorName(row)}</span>
      <span className="content-compact text-default-secondary">{t_i18n(ACTOR_TYPE_LABEL[row.actorType])}</span>
    </span>
  );

  const columns: DataTableColumn<AuditRow>[] = [
    {
      key: "at",
      header: "Time",
      cell: (r) => <span className="whitespace-nowrap">{date(r.at)}</span>,
      sortValue: (r) => (r.at ? Date.parse(r.at) : 0),
      csv: (r) => r.at ?? "",
      firstSort: "desc",
    },
    {
      key: "phase",
      header: "Phase",
      cell: (r) => t_i18n(AUDIT_PHASE_LABEL[r.phase]),
      sortValue: (r) => r.phase,
      csv: (r) => r.phase,
    },
    {
      key: "actor",
      header: "Actor",
      cell: actorChip,
      sortValue: (r) => actorName(r),
      csv: (r) => actorName(r),
    },
    {
      key: "action",
      header: "Action",
      cell: (r) => (
        <span className="flex flex-wrap items-baseline gap-x-2">{r.kind === "run" ? renderRun(r.run) : renderEvent(r.event)}</span>
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
      header: "Change",
      hideable: true,
      hideBelow: "6xl",
      cell: (r) => (r.kind === "event" && r.event.from && r.event.to && r.event.kind !== "gate" ? `${r.event.from} → ${r.event.to}` : null),
      csv: (r) => (r.kind === "event" && r.event.from && r.event.to ? `${r.event.from} -> ${r.event.to}` : ""),
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
          options={(["timeline", "table"] as const).map((key) => ({ key, label: VIEW_LABEL[key] }))}
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
          rowTestId="ctem-audit-row"
        />
      ) : (
        <div className="space-y-4" data-testid="ctem-case-history">
          {groups.length === 0 && <p className="content-base text-default-secondary">{t_i18n("Nothing happened on this case yet")}</p>}
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
                  {AUTONOMY_CLASSES.filter((c) => group.counts[c] > 0).map((c) => (
                    <Badge key={c} variant={AUTONOMY_VARIANT[c]} data-testid="ctem-audit-group-count">
                      {t_i18n(AUTONOMY_LABEL[c])} {formatCount(intl.locale, group.counts[c])}
                    </Badge>
                  ))}
                </h3>
                {!isCollapsed && (
                  <ol className="mt-2 space-y-3 border-l border-elevation-subtle pl-4">
                    {group.rows.map((row) => (
                      <li
                        key={row.key}
                        className="flex flex-wrap items-baseline gap-x-2 gap-y-1 content-base"
                        data-testid={row.kind === "run" ? "ctem-case-run" : "ctem-audit-event"}
                      >
                        {row.kind === "event" && actorChip(row)}
                        {row.kind === "run" ? renderRun(row.run) : renderEvent(row.event)}
                        {autonomyBadge(row)}
                        {row.at && <span className="ml-auto whitespace-nowrap text-default-secondary">{date(row.at)}</span>}
                        {row.kind === "event" && <GateStory event={row.event} actorName={personOf} />}
                        {row.kind === "event" && <NoteBody event={row.event} />}
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
