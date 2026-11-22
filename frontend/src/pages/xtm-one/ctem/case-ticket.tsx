// SPDX-License-Identifier: Apache-2.0

/**
 * The ticket of a case and where its sync stands (CTEM-19-UI, #1032;
 * dev-docs/ctem-ticketing.md, dev-docs/ctem-loop-visibility.md): the system,
 * the key linking to the ticket, its status, and the sync state (linked, in
 * sync, out of date since a time, error with its fix). A case with no ticket
 * says nothing unless no ticketing is declared at all: then "No ticketing
 * connected" links to the settings. Shared by the Cases board (Mobilization) and
 * the case page, so both say the same thing in the same words; the
 * `ctem_case_update` frame a sync step raises re-reads the case, so the chip
 * moves live.
 */
import { Link } from "react-router-dom";
import { Chip, Icon } from "@filigran/design-system";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import type { CtemCaseTicket, CtemTicketSyncState, CtemTicketingTarget } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** Product names of the ticketing providers the sync knows (backend ctem_ticketing.ADAPTERS). */
const SYSTEM_NAMES: Readonly<Record<string, string>> = { jira: "Jira", servicenow: "ServiceNow" };

/** The ticket status words, as the case stores them. @i18n-keys */
const TICKET_STATUS_LABELS = {
  open: "Ticket open",
  in_progress: "Ticket in progress",
  done: "Ticket done",
  cancelled: "Ticket cancelled",
} as const;

/** The sync state words. @i18n-keys */
const SYNC_LABELS = {
  linked: "Linked",
  in_sync: "In sync",
  out_of_date: "Out of date",
  error: "Sync error",
} as const;

/**
 * The fix of a sync failure, by the stable code the backend records
 * (ctem_ticketing.FIX_CODES, `sync.fix_code`): the reader's language, never the
 * backend's English `fix` sentence or its raw provider error. @i18n-keys
 */
const FIX_LABELS = {
  check_integration: "Check the ticketing integration in the settings.",
  reconnect_jira: "Reconnect the Jira integration.",
  map_jira_status: "Map a Jira status to this case status in the ticketing sync settings.",
  choose_servicenow_table: "Choose a supported ServiceNow table in the ticketing settings.",
  check_servicenow_credentials: "Check the ServiceNow credentials.",
  relink_case: "Relink the case to an existing ServiceNow record.",
} as const;

type FixCode = keyof typeof FIX_LABELS;

/** The localized fix of a failed sync: its code, else the generic fix when the record names one without a code (an older record). */
function fixSentence(sync: CtemCaseTicket["sync"], t_i18n: Translate): string | null {
  const code = sync?.fix_code;
  if (code && code in FIX_LABELS) return t_i18n(FIX_LABELS[code as FixCode]);
  return sync?.fix || sync?.error ? t_i18n(FIX_LABELS.check_integration) : null;
}

/** The tone of each sync state: a failure reads high, a lag reads medium, healthy reads low. */
const SYNC_TONE: Record<CtemTicketSyncState, "high" | "medium" | "low" | "info"> = {
  linked: "info",
  in_sync: "low",
  out_of_date: "medium",
  error: "high",
};

/** The ticket the sync manages on a case (the newest one with a sync record), else its newest reference. */
export function caseTicket(tickets: readonly CtemCaseTicket[] | null | undefined): CtemCaseTicket | null {
  if (!tickets?.length) return null;
  for (let i = tickets.length - 1; i >= 0; i -= 1) {
    if (tickets[i].sync) return tickets[i];
  }
  return tickets[tickets.length - 1];
}

/** The state a ticket shows: its sync record, "linked" for a reference the sync does not manage. */
export function ticketSyncState(ticket: CtemCaseTicket): CtemTicketSyncState {
  return ticket.sync?.state ?? "linked";
}

/** The system name of a ticket: the provider the sync recorded, else unknown (never the integration id). */
function systemName(ticket: CtemCaseTicket): string | null {
  const provider = ticket.sync?.provider;
  return provider ? (SYSTEM_NAMES[provider] ?? provider) : null;
}

/**
 * The sentence behind the sync state: since when, that it failed and how to fix
 * it, all in the reader's language. The backend's raw `error` and `fix` text
 * (English, with code values) is never drawn: it stays for agents and logs.
 */
export function ticketSyncSentence(ticket: CtemCaseTicket, t_i18n: Translate, formatDate: (iso: string) => string): string {
  const state = ticketSyncState(ticket);
  const since = ticket.sync?.since ? formatDate(ticket.sync.since) : null;
  const external = ticket.sync?.external_status;
  if (state === "out_of_date" || state === "error") {
    let head: string;
    if (state === "error") head = t_i18n("The last sync failed.");
    else head = since ? t_i18n("The ticket has not been read since {date}.", { date: since }) : t_i18n("The ticket could not be read.");
    const fix = fixSentence(ticket.sync, t_i18n);
    return fix ? `${head} ${fix}` : head;
  }
  if (state === "in_sync") {
    if (since && external) return t_i18n("In sync since {date}, status in the ticketing system: {status}.", { date: since, status: external });
    if (since) return t_i18n("In sync since {date}.", { date: since });
    return t_i18n("In sync.");
  }
  return t_i18n("Linked by hand: the sync does not manage this ticket.");
}

/**
 * The ticket chip of a case: system and key (a link when the ticket has one),
 * its status, and the sync state with its sentence on hover and focus. Nothing
 * when the case has no ticket, or "No ticketing connected" with the settings
 * link when no target is declared (`showNotConnected`, the case page).
 */
export function CaseTicketChip({
  tickets,
  ticketing,
  showNotConnected = false,
  compact = false,
}: {
  tickets: readonly CtemCaseTicket[] | null | undefined;
  ticketing?: CtemTicketingTarget;
  showNotConnected?: boolean;
  compact?: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const ticket = caseTicket(tickets);
  if (!ticket) {
    if (!showNotConnected || !ticketing || ticketing.connected) return null;
    return (
      <span className="flex items-center gap-1 content-caption text-default-secondary" data-testid="ctem-case-ticket-none">
        <Icon name="clipboard-list" size={16} aria-hidden />
        {t_i18n("No ticketing connected")}
        <Link to={ticketing.settings} className="text-default-primary underline-offset-2 hover:underline">
          {t_i18n("Connect in settings")}
        </Link>
      </span>
    );
  }
  const state = ticketSyncState(ticket);
  const system = systemName(ticket);
  const label = system ? `${system} ${ticket.key}` : ticket.key;
  const sentence = ticketSyncSentence(ticket, t_i18n, (iso) => intl.formatDate(iso, { dateStyle: "medium", timeStyle: "short" }));
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 content-caption" data-testid="ctem-case-ticket" data-sync-state={state}>
      <span className="flex min-w-0 items-center gap-1 text-default-primary">
        <Icon name="clipboard-list" size={16} aria-hidden />
        {/* Compact sits inside a card link (the board): no nested anchor, the key is text. */}
        {ticket.url && !compact ? (
          <a
            href={ticket.url}
            target="_blank"
            rel="noreferrer"
            className="truncate underline-offset-2 hover:underline"
            onClick={(e) => e.stopPropagation()}
            data-testid="ctem-case-ticket-link"
          >
            {label}
          </a>
        ) : (
          <span className="truncate">{label}</span>
        )}
      </span>
      {!compact && <span className="whitespace-nowrap text-default-secondary">{t_i18n(TICKET_STATUS_LABELS[ticket.status])}</span>}
      <Tip content={sentence}>
        <span tabIndex={tabIndex} className={cn("inline-flex", TIP_WRAP_FOCUS_RING)} data-testid="ctem-case-ticket-sync">
          <Chip label={t_i18n(SYNC_LABELS[state])} severity={SYNC_TONE[state]} className="shrink-0" />
        </span>
      </Tip>
      {state === "error" && ticketing && !compact && (
        <Link to={ticketing.settings} className="whitespace-nowrap text-default-primary underline-offset-2 hover:underline" data-testid="ctem-case-ticket-fix">
          {t_i18n("Open the ticketing settings")}
        </Link>
      )}
    </span>
  );
}
