// SPDX-License-Identifier: Apache-2.0

/**
 * The Kanban view of the CTEM Cases list (#1503, dev-docs/ctem-cases.md section 7.2):
 * the list's rows (its quick filters, search and filters applied) as columns, by
 * status in lifecycle order or by CTEM phase. Each card: title, urgency,
 * owner, SLA or due, and what a case waiting for decision waits on and since
 * when. In the status layout a card is dragged to another column to change its
 * status; a move the rules refuse (cases-kanban.kanbanDropRefusal) is refused
 * before any request with its reason, and the API's own refusal is shown the
 * same way. The columns scroll sideways inside the view, never the page.
 */
import { useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useToast } from "@/components/ui/toast";
import { useMoveCtemCase, type CtemCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { exactCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { BOARD_COLUMN_SURFACE } from "./cases-board";
import { caseGroupLabel } from "./cases-grouping";
import { kanbanColumns, kanbanDropRefusal, kanbanDropUpdate, type KanbanColumnsBy } from "./cases-kanban";
import { CaseLoopLine, CaseUrgencyChip } from "./case-loop";
import { CaseOwner, CaseStateChip } from "./cases-shared";
import { caseWaitWho } from "./case-wait";

const DRAG_TYPE = "application/x-ctem-case";

interface CasesKanbanProps {
  cases: CtemCaseSummary[];
  by: KanbanColumnsBy;
  now: number;
  meId: string | null;
  ownerName: (id: string | null) => string | null;
}

function KanbanCard({ c, by, now, ownerName, draggable }: { c: CtemCaseSummary; by: KanbanColumnsBy; now: number; ownerName: (id: string | null) => string | null; draggable: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const overdue = c.due_at && Date.parse(c.due_at) < now;
  const wait = c.state === "waiting_decision" ? c.waiting_on : null;
  // Never the raw `by` (UX r1 B3 on #1515): a person by name, else "a person" or "a decision".
  const who = wait ? caseWaitWho(wait.by, ownerName) : null;
  const since = wait?.since ? intl.formatDate(wait.since, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : null;
  return (
    <li
      draggable={draggable}
      onDragStart={(event) => {
        event.dataTransfer.setData(DRAG_TYPE, c.id);
        event.dataTransfer.effectAllowed = "move";
      }}
      data-testid="ctem-cases-kanban-card"
      data-case={c.id}
      className={cn("rounded-sm border border-elevation-subtle bg-elevation-default", draggable && "cursor-grab active:cursor-grabbing")}
    >
      <Link
        to={`/ctem/cases/${c.id}`}
        className="flex min-w-0 flex-col gap-1.5 p-2.5 hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <TruncatedText text={c.title} className="content-base font-medium" />
        <span className="flex min-w-0 flex-wrap items-center gap-2 content-caption text-default-secondary">
          <CaseUrgencyChip severity={c.severity} priority={c.priority} />
          {by === "phase" && <CaseStateChip state={c.state} />}
          {c.due_at && (
            <span className={cn("whitespace-nowrap", overdue && "text-feedback-error-tertiary")}>
              {t_i18n("Due {date}", { date: intl.formatDate(c.due_at, { day: "numeric", month: "short" }) })}
            </span>
          )}
        </span>
        <span className="min-w-0 content-caption">
          <CaseOwner name={ownerName(c.owner_user_id)} />
        </span>
        {wait && (
          <span className="flex min-w-0 items-center gap-1 content-caption text-feedback-warning-primary" data-testid="ctem-cases-kanban-waits">
            <Icon name="clock" size={16} className="shrink-0" aria-hidden />
            <TruncatedText
              text={
                who?.kind === "name"
                  ? since
                    ? t_i18n("Waits on {who} since {date}", { who: who.name, date: since })
                    : t_i18n("Waits on {who}", { who: who.name })
                  : who?.kind === "person"
                    ? since
                      ? t_i18n("Waits on a person since {date}", { date: since })
                      : t_i18n("Waits on a person")
                    : since
                      ? t_i18n("Waits on a decision since {date}", { date: since })
                      : t_i18n("Waits on a decision")
              }
            />
          </span>
        )}
        <CaseLoopLine c={c} now={now} />
      </Link>
    </li>
  );
}

export function CasesKanban({ cases, by, now, meId, ownerName }: CasesKanbanProps) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const move = useMoveCtemCase();
  const [over, setOver] = useState<string | null>(null);
  const columns = kanbanColumns(by, cases);
  const draggable = by === "state";

  const drop = (target: string) => (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setOver(null);
    const id = event.dataTransfer.getData(DRAG_TYPE);
    const c = cases.find((k) => k.id === id);
    if (!c || !draggable || c.state === target) return;
    const state = target as CtemCaseState;
    const refused = kanbanDropRefusal(c, state, meId);
    if (refused) {
      toast({ title: t_i18n("Move refused"), description: t_i18n(refused), variant: "error" });
      return;
    }
    move.mutate(
      { id: c.id, ...kanbanDropUpdate(c, state, meId) },
      { onError: (err) => toast({ title: t_i18n("Move refused"), description: err.message, variant: "error" }) },
    );
  };

  return (
    <div className="overflow-x-auto pb-2" data-testid="ctem-cases-kanban" data-columns-by={by}>
      <div className="flex min-w-max gap-3">
        {[...columns.entries()].map(([key, list]) => {
          const label = t_i18n(caseGroupLabel(by, key));
          return (
            <section
              key={key || "none"}
              aria-label={label}
              data-testid={`ctem-cases-kanban-${key || "none"}`}
              onDragOver={draggable ? (event) => { event.preventDefault(); setOver(key); } : undefined}
              onDragLeave={draggable ? () => setOver((current) => (current === key ? null : current)) : undefined}
              onDrop={draggable ? drop(key) : undefined}
              className={cn(
                "flex w-72 shrink-0 flex-col gap-2 rounded-sm border border-elevation-subtle p-2.5",
                BOARD_COLUMN_SURFACE,
                over === key && "ring-2 ring-focus",
              )}
            >
              <header className="flex items-center justify-between gap-2">
                {by === "state" ? <CaseStateChip state={key as CtemCaseState} /> : <span className="content-base font-semibold">{label}</span>}
                <span className="content-base font-semibold tabular-nums">{exactCount(intl.locale, list.length)}</span>
              </header>
              {list.length === 0 ? (
                <p className="py-4 text-center content-caption text-default-secondary">{t_i18n("No case")}</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {list.map((c) => (
                    <KanbanCard key={c.id} c={c} by={by} now={now} ownerName={ownerName} draggable={draggable} />
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
