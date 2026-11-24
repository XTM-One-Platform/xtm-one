// SPDX-License-Identifier: Apache-2.0

/**
 * One CTEM case `/ctem/cases/:id` (dev-docs/ctem-cases.md section 7.3): the
 * fix as title, its state actions, the summary, the issues it closes and its
 * history, as the caller sees them (#11613). A member issue the caller cannot
 * see in its source product is a "Restricted" row in the same layout and at
 * the same height as a visible row (OpenCTI's pattern), with nothing about it
 * but that word; a case the caller sees nothing of answers 404.
 */
import { useMemo, useState, type ComponentProps } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Icon, Textarea } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { ModulePivotLink } from "@/components/ui/module-pivot-link";
import { SourceBadge } from "@/components/ui/source-badge";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NotFoundState } from "@/components/ui/not-found-state";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { PageSkeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import { useToast } from "@/components/ui/toast";
import { Input } from "@/components/ui/input";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useMe, useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import {
  isRestrictedIssue,
  useCtemCase,
  useCtemCaseRuns,
  useSplitCtemCase,
  useUpdateCtemCase,
  type CtemCaseEvent,
  type CtemCaseIssue,
  type CtemCaseRun,
  type CtemCaseUpdate,
  type CtemCaseVisibleIssue,
} from "@/hooks/api/useCtemCasesApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { CASE_RUN_LINE, caseRunLine } from "@/lib/ctem-case-activity";
import { auditChange, auditRows, personReason, reasonLabel, type AuditRow } from "@/lib/ctem-case-audit";
import { CaseAuditTrail } from "./ctem/CaseAuditTrail";
import { timelineOutcome, type TimelineOutcome } from "@/lib/ctem-fleet-timeline";
import { isNotFoundError } from "@/lib/api";
import { exactCount, formatCount } from "@/lib/number-format";
import { useNow } from "@/hooks/useTick";
import { CaseLoopLine, CaseUrgencyChip, RetestChip, hasRetestData, retestRank } from "./ctem/case-loop";
import { CaseTicketChip } from "./ctem/case-ticket";
import { keyVersion } from "./ctem/case-version";
import { CaseAttackPath } from "./ctem/CaseAttackPath";
import { CaseExploitation } from "./ctem/CaseExploitation";
import { CASE_EVENT_LABEL } from "./ctem/case-events";
import { GATE_LABEL, GATE_OUTCOME_LABEL, GATE_REASON_LABEL, gateReason, isGateOutcome, isUserId } from "./ctem/case-gates";
import { CaseRemediationDecision } from "./ctem/CaseRemediationDecision";
import { CaseAnalystReview } from "./ctem/CaseAnalystReview";
import { CaseValidation } from "./ctem/CaseValidation";
import { CaseTreatmentPlan } from "./ctem/CaseTreatmentPlan";
import { CaseRunningVersions } from "./ctem/CaseRunningVersions";
import { CaseAffectedAssets } from "./ctem/CaseAffectedAssets";
import { CaseOpenedBy } from "./ctem/CaseOpenedBy";
import { CaseOwnedBy } from "./ctem/CaseOwnedBy";
import { CaseLoopProvider } from "./ctem/CaseSectionReason";
import { loopFleetOf } from "./ctem/case-loop-reason";
import { caseAutomations, caseSources, caseWork } from "./ctem/case-workers";
import { CaseWorkCard } from "./ctem/CaseWorkCard";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useAssignments } from "@/hooks/api/useAssignmentApi";
import { useAgents } from "@/hooks/api/useAgentApi";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { useModuleBaseUrls } from "@/hooks/useModuleBaseUrls";
import { pivotModuleOf } from "@/lib/module-pivot";
import { CaseAttentionNow } from "./ctem/CaseAttentionNow";
import { CASE_STATE_LABEL, CaseOwner, CaseStateChip } from "./ctem/cases-shared";
import { EntityPageHeader } from "@/components/ui/entity-page-header";

/** A table row: the issue and its place, so restricted rows keep a stable key. */
interface IssueRow {
  key: string;
  issue: CtemCaseIssue;
}

const REASON_MAX = 500;

/** The day a date input holds, `yyyy-mm-dd`, as the end of that day in UTC. */
function endOfDayIso(day: string): string {
  return `${day}T23:59:59Z`;
}

function inDays(days: number): string {
  const d = new Date(Date.now() + days * 86_400_000);
  return d.toISOString().slice(0, 10);
}

/* Library glyphs (no direct lucide-react import, src/no-direct-lucide-import.test.ts) handed to
   the shared components that take a glyph component: they size them through className. */
type Glyph = ComponentProps<typeof StatCard>["icon"];
const CaseGlyph = (({ className }: { className?: string }) => <Icon name="briefcase" size={16} className={className} />) as unknown as Glyph;
const IssuesGlyph = (({ className }: { className?: string }) => <Icon name="activity" size={16} className={className} />) as unknown as Glyph;
const KevGlyph = (({ className }: { className?: string }) => <Icon name="triangle-alert" size={16} className={className} />) as unknown as Glyph;
const ScoreGlyph = (({ className }: { className?: string }) => <Icon name="thermometer" size={16} className={className} />) as unknown as Glyph;
const AssetsGlyph = (({ className }: { className?: string }) => <Icon name="server" size={16} className={className} />) as unknown as Glyph;

/** The cell of a restricted row: the word, nothing about the issue (#11613). */
function RestrictedCell() {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex items-center gap-1.5 text-default-secondary" data-testid="ctem-case-restricted">
      <Icon name="lock" size={16} className="shrink-0" />
      {t_i18n("Restricted")}
    </span>
  );
}

/** The status word of a `ticket` history row, translated where drawn (#1032). */
const TICKET_EVENT_STATUS: Record<string, string> = {
  open: "Open (status)",
  in_progress: "In progress",
  done: "Done",
  cancelled: "Cancelled",
};

/** The ticket a history row names (`integration:key`), its key alone: never the integration id. */
function ticketKeyOf(evidence: string | null): string | null {
  if (!evidence) return null;
  const at = evidence.indexOf(":");
  return at >= 0 ? evidence.slice(at + 1) || null : evidence;
}

/** The OpenAEV event of a `source_event` history row, translated where drawn (#956). */
const SOURCE_EVENT_LABEL: Record<string, string> = {
  simulation_finished: "Simulation finished",
  finding_updated: "Finding updated",
  finding_deleted: "Finding deleted",
  // The simulation's attack path reached a high or very high criticality asset (CTEM-13-UI, #1142).
  critical_asset_reached: "Critical asset reached",
};

/** The result word of a validation history row, translated where drawn. */
const VALIDATION_RESULT_LABEL: Record<string, string> = {
  passed: "Passed",
  not_exposed: "Not exposed",
  exposed: "Exposed",
  failed: "Failed",
  pending: "Pending",
};

/**
 * The word of one side of an event's from -> to, through the labels its action uses
 * (UX round 1 on #1520, B3): never a raw key such as `needs_attention`; null when the
 * value has no label (UX round 2, B1), so the row shows no change rather than a key.
 */
function changeWord(kind: string, value: string): string | null {
  if (kind === "ticket") return TICKET_EVENT_STATUS[value] ?? null;
  if (kind === "validation") return VALIDATION_RESULT_LABEL[value] ?? null;
  return CASE_STATE_LABEL[value as keyof typeof CASE_STATE_LABEL] ?? null;
}

/**
 * The badge of a fleet run on the case history, the keys the fleet timeline
 * uses (one run). @i18n-keys
 */
const RUN_OUTCOME_LABELS = {
  succeeded: "Run outcome: succeeded",
  failed: "Run outcome: failed",
  running: "Run outcome: running",
  pending: "Run outcome: pending",
  interrupted: "Run outcome: interrupted",
  cancelled: "Run outcome: cancelled",
} as const satisfies Record<TimelineOutcome, string>;

const RUN_OUTCOME_VARIANTS: Partial<Record<TimelineOutcome, "success" | "warning" | "destructive" | "secondary">> = {
  succeeded: "success",
  failed: "destructive",
  interrupted: "warning",
};

type DialogKind = "start" | "resolve" | "accept" | "reopen" | "split" | null;

/**
 * The fixed version of one issue and where OpenCTI's knowledge came from (CTEM-11, #946; shown by #928):
 * the remediating Software, else the course of action, else "unknown"; a declared version OpenCTI
 * holds no Software of is named, never guessed.
 */
function IssueVersion({ issue }: { issue: CtemCaseVisibleIssue }) {
  const { t_i18n } = useFormatter();
  if (issue.not_affected || issue.source !== "opencti") return null;
  const unknownDeclared = issue.declared_version_known === false ? issue.installed_version : null;
  const secondary =
    issue.fixed_version && issue.fixed_by
      ? t_i18n("Matched on Software {software}", { software: issue.fixed_by })
      : issue.fixed_version && issue.course_of_action_name
        ? t_i18n("Named by {courseOfAction}", { courseOfAction: issue.course_of_action_name })
        : unknownDeclared
          ? t_i18n("Version {version} not known to OpenCTI", { version: unknownDeclared })
          : null;
  return (
    <span className="flex min-w-0 flex-col" data-testid="ctem-case-issue-version">
      <span>{issue.fixed_version ?? t_i18n("Unknown")}</span>
      {secondary && <IssueCaption text={secondary} />}
    </span>
  );
}

/**
 * The secondary line of an Issues cell (UX round 3 B1 and round 4 B1 on #931): one line, cut
 * with an ellipsis, the full sentence in the tooltip, so every row keeps the two-line height at
 * 1080 and 1728 (Design standards s.3). The shared TruncatedText draws the design-system
 * tooltip only when the line is cut. The cap gives the auto-layout cell a width (13.ak) and
 * follows the table's own container: 9 rem below 72 rem, so the Fixed version and State
 * columns hold about 168 px each and the table fits a 1080 screen without a sideways scroll;
 * 24 rem (384 px) from 72 rem, about the content width of each column at 1728, so a caption
 * that fits its cell reads whole (UX round 5 B1: 18 rem cut a 371 px caption in a 375 px cell).
 */
function IssueCaption({ text }: { text: string }) {
  return <TruncatedText text={text} className="max-w-[9rem] @6xl:max-w-[24rem] content-compact text-default-secondary" />;
}

export default function CtemCaseDetail() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { data: me } = useMe();
  const { data: kase, isLoading, error, isError, refetch } = useCtemCase(id);
  const agenticLicensed = useAgenticAIEnabled();
  const { data: caseRuns } = useCtemCaseRuns(id, agenticLicensed);
  // The History as an audit trail grouped by CTEM phase (#1490): events and fleet runs, each with its phase and autonomy class.
  const activity = useMemo(() => auditRows(kase?.history ?? [], caseRuns?.runs), [kase, caseRuns]);
  // Who works the case (#1266): every automation on the case events, whatever fleet or agent
  // owns it, and the runs those events started here; the products each section is written from.
  const fleets = useFleets({ optional: true, enabled: agenticLicensed });
  const loopFleet = loopFleetOf(fleets.data);
  const assignments = useAssignments();
  const agents = useAgents();
  const { data: registrations } = usePlatformRegistrations();
  const work = useMemo(
    () =>
      caseWork({
        licensed: agenticLicensed,
        // Only the automations that can start on THIS case: its events and its filter (UX round 1 on #1275, B1).
        automations:
          assignments.isError || assignments.data === undefined || !kase
            ? undefined
            : caseAutomations(assignments.data, agents.data, { id: kase.id, trigger_path: kase.trigger_path }),
        runs: caseRuns?.runs,
      }),
    [agenticLicensed, assignments.data, assignments.isError, agents.data, caseRuns, kase],
  );
  const loopState = useMemo(() => ({ sources: caseSources(registrations) }), [registrations]);
  const moduleUrls = useModuleBaseUrls();
  const update = useUpdateCtemCase(id);
  const split = useSplitCtemCase();
  const [picked, setPicked] = useState<string[]>([]);
  const [splitTitle, setSplitTitle] = useState("");
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [expiry, setExpiry] = useState(inDays(90));
  const [formError, setFormError] = useState<string | null>(null);
  // The SLA countdown of the header moves without a reload (#892).
  const now = useNow(60_000);

  const actorIds = useMemo(() => {
    const ids = new Set<string>();
    if (kase?.owner_user_id) ids.add(kase.owner_user_id);
    for (const e of kase?.history ?? []) {
      if (/^[0-9a-f-]{36}$/i.test(e.actor)) ids.add(e.actor);
      // A gate answered by a person names them by id in its reason.
      if (e.kind === "gate" && e.to === "answered" && isUserId(e.reason)) ids.add(e.reason);
      // An owner change names both owners by id (UX round 2 on #1520, B1).
      if (e.kind === "owner") for (const v of [e.from, e.to]) if (v && isUserId(v)) ids.add(v);
    }
    return [...ids];
  }, [kase]);
  const { data: people } = useUserDirectoryByIds(actorIds);
  const personName = (personId: string | null | undefined) =>
    personId ? (people?.find((p) => p.id === personId)?.full_name ?? null) : null;

  // The audit trail's words (#1490): who acted, and what each event or run did, the long body of a note left to its expand.
  const actorNameOf = (row: AuditRow): string => {
    if (row.kind === "run") return row.run.agent_name ?? t_i18n("Agent");
    const e = row.event;
    if (row.actorType === "system") return t_i18n("System");
    return personName(e.actor) ?? e.actor_name ?? t_i18n(row.actorType === "person" ? "A person" : "Agent");
  };
  const eventLabel = (e: CtemCaseEvent) =>
    e.kind === "ticket" && !e.from ? t_i18n("Ticket created") : t_i18n(CASE_EVENT_LABEL[e.kind] ?? "Changed the case");
  // The from -> to in words (UX round 2 on #1520, B1): the owner by name, the due date in
  // the user's locale on screen and as an ISO date in the CSV, a state by its label.
  const changeTextOf = (e: CtemCaseEvent, forCsv = false) =>
    auditChange(e, {
      word: (kind, value) => {
        const word = changeWord(kind, value);
        return word === null ? null : t_i18n(word);
      },
      person: (personId) => personName(personId) ?? t_i18n("a person"),
      date: (iso) => (forCsv ? iso.slice(0, 10) : intl.formatDate(iso, { dateStyle: "medium" })),
    });
  const actionTextOf = (row: AuditRow): string => {
    if (row.kind === "run") return t_i18n(CASE_RUN_LINE[caseRunLine(timelineOutcome(row.run.outcome, row.run.status))]);
    const e = row.event;
    // "Changed the state to" carries its target, as the Timeline does, so the Action
    // reads whole when the Change column is hidden (UX round 2 on #1520, B3).
    if (e.kind === "state" && e.to) {
      const target = changeWord("state", e.to);
      return target ? `${eventLabel(e)} ${t_i18n(target)}` : eventLabel(e);
    }
    const said = reasonLabel(e);
    const detail =
      e.kind === "gate" ? (e.from ? t_i18n(GATE_LABEL[e.from] ?? e.from) : "") : said ? t_i18n(said) : e.kind === "note" ? (e.headline ?? "") : "";
    return detail ? `${eventLabel(e)}: ${detail}` : eventLabel(e);
  };
  const renderRun = (run: CtemCaseRun) => {
    const outcome = timelineOutcome(run.outcome, run.status);
    return (
      <>
        <span className="font-medium">{run.agent_name ?? t_i18n("Agent")}</span>
        <Link
          to={`/operations/executions/${run.id}`}
          state={{ backTo: `/ctem/cases/${id}`, backLabel: "Back to Case" }}
          aria-label={t_i18n("Open run of {agent}", { agent: run.agent_name ?? t_i18n("Agent") })}
          className="hover:underline"
        >
          {t_i18n(CASE_RUN_LINE[caseRunLine(outcome)])}
        </Link>
        {outcome && (
          <Badge variant={RUN_OUTCOME_VARIANTS[outcome] ?? "secondary"} className="shrink-0">
            {t_i18n(RUN_OUTCOME_LABELS[outcome])}
          </Badge>
        )}
      </>
    );
  };
  const renderEvent = (e: CtemCaseEvent) => {
    const why = e.kind === "gate" ? gateReason(e.reason, e.to) : null;
    return (
      <>
        <span>{eventLabel(e)}</span>
        {(e.kind === "ticket" || e.kind === "retest_requested") && ticketKeyOf(e.evidence) && (
          <span className="font-medium" data-testid="ctem-case-ticket-event">{ticketKeyOf(e.evidence)}</span>
        )}
        {e.kind === "ticket" && e.from && e.to && (
          <span>
            {t_i18n("{from} to {to}", {
              from: t_i18n(TICKET_EVENT_STATUS[e.from] ?? e.from),
              to: t_i18n(TICKET_EVENT_STATUS[e.to] ?? e.to),
            })}
          </span>
        )}
        {e.issue === "Restricted" && <span className="text-default-secondary">{t_i18n("Restricted")}</span>}
        {e.to && e.kind === "state" && <span>{t_i18n(CASE_STATE_LABEL[e.to as keyof typeof CASE_STATE_LABEL] ?? e.to)}</span>}
        {e.kind === "retargeted" && keyVersion(e.to) && <span>{keyVersion(e.to)}</span>}
        {e.to && e.kind === "validation" && <span>{t_i18n(VALIDATION_RESULT_LABEL[e.to] ?? e.to)}</span>}
        {e.to && e.kind === "source_event" && (
          <span data-testid="ctem-case-source-event">{t_i18n(SOURCE_EVENT_LABEL[e.to] ?? e.to)}</span>
        )}
        {e.kind === "gate" && e.from && <span data-testid="ctem-case-gate">{t_i18n(GATE_LABEL[e.from] ?? e.from)}</span>}
        {e.kind === "gate" && isGateOutcome(e.to) && <span className="font-medium">{t_i18n(GATE_OUTCOME_LABEL[e.to])}</span>}
        {e.kind === "gate" && e.to === "answered" && (
          <span className="text-default-secondary">
            {t_i18n("by {name}", { name: (e.reason && personName(e.reason)) || t_i18n("a person") })}
          </span>
        )}
        {why && <span className="text-default-secondary">{t_i18n(GATE_REASON_LABEL[why.kind], { runbook: why.runbook ?? "" })}</span>}
        {/* A note reads its headline, its body behind "Show more" (#1490); any other reason is short. */}
        {e.kind === "note"
          ? (e.headline ?? e.reason) && <span className="text-default-secondary" data-testid="ctem-audit-headline">{e.headline ?? e.reason}</span>
          : e.kind !== "gate" &&
            (reasonLabel(e) || personReason(e)) && (
              <span className="text-default-secondary" data-testid="ctem-audit-reason">
                {reasonLabel(e) ? t_i18n(reasonLabel(e) ?? "") : personReason(e)}
              </span>
            )}
        {e.evidence && e.kind !== "ticket" && e.kind !== "retest_requested" && e.kind !== "treatment_plan" && e.kind !== "threat_package" && (
          <span className="text-default-secondary">{e.kind === "gate" ? t_i18n("Option {option}", { option: e.evidence }) : e.evidence}</span>
        )}
      </>
    );
  };

  const issueRows: IssueRow[] = useMemo(
    () => (kase?.issues ?? []).map((issue, i) => ({ key: isRestrictedIssue(issue) ? `restricted-${i}` : `${issue.source}:${issue.external_id}`, issue })),
    [kase],
  );
  // Members OpenCTI relates to none of the declared versions (CTEM-11, #946; shown by #928), from the viewer's own read.
  const notAffectedOf = (r: IssueRow) => (isRestrictedIssue(r.issue) || r.issue.open || !r.issue.not_affected ? undefined : r.issue.installed_version || undefined);

  if (isNotFoundError(error)) {
    return (
      <NotFoundState
        icon={CaseGlyph}
        title="Case not found"
        description="This case does not exist, or none of its issues is visible to you in OpenCTI or OpenAEV."
        actionLabel="Back to Cases"
        onAction={() => navigate("/ctem/cases")}
        backTo="/ctem/cases"
        backLabel="Back to Cases"
      />
    );
  }
  if (isLoading && !kase) return <PageSkeleton cards={3} />;
  if (!kase) {
    return (
      <Card>
        <CardContent className="flex items-center justify-between gap-4 p-6">
          <span>{t_i18n("This case could not be read.")}</span>
          {isError && <Button variant="outline" onClick={() => void refetch()}>{t_i18n("Retry")}</Button>}
        </CardContent>
      </Card>
    );
  }

  const open = (kind: DialogKind) => {
    setReason("");
    setEvidence("");
    setExpiry(inDays(90));
    setFormError(null);
    setSplitTitle("");
    setDialog(kind);
  };

  // Split (dev-docs/ctem-cases.md section 5): the open issues picked move to a new
  // manual case that pins them; recompute keeps them there.
  const splitOut = () =>
    split.mutate(
      { title: splitTitle.trim(), issue_ids: picked },
      {
        onSuccess: (created) => {
          setDialog(null);
          setPicked([]);
          toast({ title: t_i18n("Issues split into a new case"), variant: "success" });
          navigate(`/ctem/cases/${created.id}`);
        },
        onError: (err) => setFormError(err.message),
      },
    );

  const submit = (body: CtemCaseUpdate) =>
    update.mutate(body, {
      onSuccess: () => {
        setDialog(null);
        toast({ title: t_i18n("Case updated"), variant: "success" });
      },
      // The API answers 422 with the rule in plain words (owner, reason, evidence, expiry).
      onError: (err) => setFormError(err.message),
    });

  const ownerName = personName(kase.owner_user_id);
  const restricted = kase.restricted_count;
  // The Re-test column exists only when a member carries the OpenAEV lifecycle,
  // so an older case looks the same as before (UX round 1 on #962, B1).
  const showRetest = hasRetestData(kase.issues ?? []);

  const columns: DataTableColumn<IssueRow>[] = [
    {
      key: "issue",
      header: "Issue",
      cell: (r) =>
        isRestrictedIssue(r.issue) ? (
          <RestrictedCell />
        ) : (
          // The issue, then "Open in <Module>" on the entity it came from (#1357): an OpenCTI
          // vulnerability by its id, an OpenAEV finding by its id; a signal has no module page.
          <span className="flex min-w-0 max-w-[26rem] items-center gap-1.5">
            <span className="truncate font-medium" title={r.issue.name ?? r.issue.external_id}>
              {r.issue.cve ?? r.issue.name ?? r.issue.external_id}
            </span>
            <ModulePivotLink
              module={pivotModuleOf(r.issue.source)}
              baseUrl={moduleUrls[pivotModuleOf(r.issue.source) ?? "opencti"]}
              id={r.issue.external_id}
              kind={pivotModuleOf(r.issue.source) === "openaev" ? "finding" : "entity"}
              iconOnly
              testId="ctem-case-issue-pivot"
            />
          </span>
        ),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? "~" : (r.issue.cve ?? r.issue.name ?? "")),
      csv: (r) => (isRestrictedIssue(r.issue) ? t_i18n("Restricted") : (r.issue.cve ?? r.issue.name ?? "")),
    },
    {
      key: "source",
      header: "Source",
      // The module's emblem and name (#1357); a signal keeps its name alone.
      cell: (r) =>
        isRestrictedIssue(r.issue) ? null : (
          <SourceBadge source={r.issue.source === "signal" ? "signal" : (pivotModuleOf(r.issue.source) ?? "opencti")} label={t_i18n("Signal")} testId="ctem-case-issue-source" />
        ),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? "~" : r.issue.source),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : r.issue.source),
    },
    {
      key: "cvss",
      header: "CVSS",
      align: "right",
      cell: (r) => (isRestrictedIssue(r.issue) || r.issue.cvss == null ? null : formatCount(intl.locale, r.issue.cvss)),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? -1 : (r.issue.cvss ?? -1)),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : (r.issue.cvss ?? "")),
      firstSort: "desc",
    },
    {
      key: "epss",
      header: "EPSS",
      align: "right",
      cell: (r) => (isRestrictedIssue(r.issue) || r.issue.epss == null ? null : exactCount(intl.locale, r.issue.epss)),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? -1 : (r.issue.epss ?? -1)),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : (r.issue.epss ?? "")),
      firstSort: "desc",
    },
    {
      key: "kev",
      header: "KEV",
      cell: (r) => (isRestrictedIssue(r.issue) ? null : r.issue.kev ? t_i18n("Yes") : t_i18n("No")),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? -1 : r.issue.kev ? 1 : 0),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : r.issue.kev ? "yes" : "no"),
      hideBelow: "3xl",
      hideable: true,
    },
    {
      key: "assets",
      header: "Assets",
      align: "right",
      cell: (r) => (isRestrictedIssue(r.issue) ? null : formatCount(intl.locale, r.issue.assets?.length ?? 0)),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? -1 : (r.issue.assets?.length ?? 0)),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : (r.issue.assets ?? []).join("; ")),
      hideBelow: "4xl",
      hideable: true,
    },
    ...(showRetest
      ? [
          {
            key: "retest",
            header: "Re-test",
            cell: (r: IssueRow) =>
              isRestrictedIssue(r.issue) ? null : (
                <span className="flex flex-col items-start gap-0.5">
                  <RetestChip issue={r.issue} />
                  {r.issue.last_tested_at && (
                    <span className="content-caption text-default-secondary">
                      <DateTimeCell value={r.issue.last_tested_at} testId="ctem-case-issue-last-tested" />
                    </span>
                  )}
                </span>
              ),
            sortValue: (r: IssueRow) => (isRestrictedIssue(r.issue) ? 99 : retestRank(r.issue)),
            csv: (r: IssueRow) => (isRestrictedIssue(r.issue) ? "" : (r.issue.retest_status ?? (r.issue.retest_pending ? "queued" : ""))),
            hideable: true,
          } satisfies DataTableColumn<IssueRow>,
        ]
      : []),
    {
      key: "version",
      header: "Fixed version",
      cell: (r) => (isRestrictedIssue(r.issue) ? null : <IssueVersion issue={r.issue} />),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? "~" : (r.issue.fixed_version ?? "~")),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : (r.issue.fixed_version ?? "")),
      hideBelow: "xl",
      hideable: true,
    },
    {
      key: "state",
      header: "State",
      cell: (r) => {
        if (isRestrictedIssue(r.issue)) return null;
        if (r.issue.open) return t_i18n("Open (status)");
        const version = notAffectedOf(r);
        return version ? (
          <span className="flex min-w-0 flex-col" data-testid="ctem-case-not-affected">
            <span>{t_i18n("Not affected")}</span>
            <IssueCaption text={t_i18n("OpenCTI relates no vulnerability to version {version}", { version })} />
          </span>
        ) : (
          t_i18n("Closed")
        );
      },
      sortValue: (r) => (isRestrictedIssue(r.issue) ? 3 : r.issue.open ? 0 : notAffectedOf(r) ? 2 : 1),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : r.issue.open ? "open" : notAffectedOf(r) ? "not affected" : "closed"),
    },
  ];

  // The next state as the filled primary, the platform order (secondary first).
  // Before remediation a person starts it (with an owner, #1503); in remediation or validation they resolve it.
  const notStarted = kase.state === "new" || kase.state === "analyzing" || kase.state === "waiting_decision";
  const primary = notStarted
    ? { label: t_i18n("Start"), kind: "start" as const }
    : kase.state === "remediating" || kase.state === "validating"
      ? { label: t_i18n("Resolve"), kind: "resolve" as const }
      : { label: t_i18n("Reopen"), kind: "reopen" as const };

  const dialogTitle: Record<Exclude<DialogKind, null>, string> = {
    start: t_i18n("Start this case"),
    resolve: t_i18n("Resolve this case"),
    accept: t_i18n("Accept risk"),
    reopen: t_i18n("Reopen this case"),
    split: t_i18n("{count, plural, one {Split # issue into a new case} other {Split # issues into a new case}}", { count: picked.length }),
  };

  const confirm = () => {
    if (dialog === "split") splitOut();
    else if (dialog === "start") submit({ state: "remediating", owner_user_id: kase.owner_user_id ?? me?.id });
    else if (dialog === "resolve") submit({ state: "resolved", evidence: evidence.trim() });
    else if (dialog === "reopen") submit({ state: "new", reason: reason.trim() });
    else if (dialog === "accept")
      submit({
        state: "accepted_risk",
        owner_user_id: kase.owner_user_id ?? me?.id,
        reason: reason.trim(),
        accepted_until: endOfDayIso(expiry),
      });
  };

  return (
    <div className="space-y-6">
      {/* The app's standard entity header (#1367, Sam 23 Nov 2026: no band with a surface and a border): the
          back link, the title, state, priority and owner on its meta row, the actions on the title line. */}
      <EntityPageHeader
        backTo="/ctem/cases"
        backLabel="Back to Cases"
        badges={
          <>
            <CaseStateChip state={kase.state} />
            <CaseUrgencyChip severity={kase.severity} priority={kase.priority} />
            <CaseOwner name={ownerName} />
            <CaseOwnedBy owner={kase.owner_fleet} fleets={fleets.data} caseId={kase.id} />
            <CaseLoopLine c={kase} now={now} />
            <CaseTicketChip tickets={kase.tickets} ticketing={kase.ticketing} showNotConnected />
          </>
        }
        title={kase.title}
        wrapTitle
        hint={t_i18n("Open issues grouped by the fix that closes them. You see the cases holding at least one issue you can see in OpenCTI or OpenAEV.")}
        hintLabel={t_i18n("About cases")}
        actions={
          <>
            {kase.state !== "accepted_risk" && kase.state !== "resolved" && (
              <Button variant="outline" onClick={() => open("accept")}>
                {t_i18n("Accept risk")}
              </Button>
            )}
            <Button onClick={() => open(primary.kind)}>
              {primary.label}
            </Button>
          </>
        }
      />

      {/* Why now, created, age, last change and due (#1176): the same explained score as the Cases list. */}
      <CaseAttentionNow c={kase} now={now} onChanged={() => void refetch()} />

      {kase.state === "accepted_risk" && (
        <Card>
          <CardContent className="space-y-1 p-4 content-base">
            <div className="font-medium">{t_i18n(CASE_STATE_LABEL.accepted_risk)}</div>
            {kase.accepted_reason && <div className="text-default-secondary">{kase.accepted_reason}</div>}
            {kase.accepted_until && (
              <div className="text-default-secondary">
                {t_i18n("Expires on {date}", { date: intl.formatDate(kase.accepted_until, { dateStyle: "medium" }) })}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <StatCard
          icon={IssuesGlyph}
          label={t_i18n("Open issues")}
          value={kase.open_issue_count}
          subtitle={restricted > 0 ? t_i18n("{count, plural, one {# restricted} other {# restricted}}", { count: restricted }) : t_i18n("All visible to you")}
        />
        <StatCard icon={KevGlyph} label={t_i18n("KEV")} value={kase.kev_count} subtitle={t_i18n("On the issues you can see")} />
        <StatCard
          icon={ScoreGlyph}
          label={t_i18n("Max CVSS")}
          value={formatCount(intl.locale, kase.max_cvss)}
          subtitle={t_i18n("On the issues you can see")}
        />
        <StatCard icon={AssetsGlyph} label={t_i18n("Assets")} value={kase.asset_count} subtitle={t_i18n("{count, plural, one {# crown jewel} other {# crown jewels}}", { count: kase.crown_jewel_count })} />
      </div>

      {/* The running versions come from the asset inventory, never typed by hand in the software inventory (#1252). */}
      {kase.remediation_kind === "upgrade" && (
        <CaseRunningVersions productLabel={kase.product_label} fixedVersion={kase.fixed_version} versions={kase.running_versions} />
      )}

      {/* The assets the visible issues name, each with the version it runs from the inventory (#1176 slice 3b). */}
      <CaseAffectedAssets data={kase.affected_assets} />

      {kase.details_unavailable.length > 0 && (
        <p className="content-compact text-default-secondary">
          {t_i18n("{products} did not answer: details of its issues are unavailable.", { products: kase.details_unavailable.join(", ") })}
        </p>
      )}

      {kase.state !== "resolved" && kase.state !== "accepted_risk" && (
        <CaseWorkCard
          work={work}
          loopFleetId={loopFleet?.id ?? null}
          onStart={notStarted ? () => open("start") : undefined}
        />
      )}

      <CaseLoopProvider value={loopState}>
        <CaseOpenedBy caseId={kase.id} history={kase.history} />

        <CaseExploitation entries={kase.exploitation} />

        <CaseAttackPath reading={kase.attack_path} />

        <CaseAnalystReview caseId={kase.id} pkg={kase.threat_package} history={kase.history} gates={kase.gates} personName={personName} />

        <CaseValidation caseId={kase.id} validations={kase.validations} history={kase.history} gates={kase.gates} personName={personName} />

        <CaseRemediationDecision caseId={kase.id} history={kase.history} gates={kase.gates} personName={personName} />

        <CaseTreatmentPlan plan={kase.treatment_plan} />
      </CaseLoopProvider>

      <Card>
        <CardHeader>
          <CardTitle>{t_i18n("Issues")}</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            framed={false}
            rows={issueRows}
            columns={columns}
            rowKey={(r) => r.key}
            selection={{
              selected: picked,
              onChange: setPicked,
              // Only an open issue the caller sees can leave the case (#11613).
              disabledReason: (key) => {
                const row = issueRows.find((r) => r.key === key);
                if (!row || isRestrictedIssue(row.issue)) return t_i18n("Restricted");
                return row.issue.open ? undefined : t_i18n("A closed issue stays with its case");
              },
              actions: (
                <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("split")} data-testid="ctem-case-split">
                  {t_i18n("Split into a new case")}
                </Button>
              ),
            }}
            defaultSort={{ key: "cvss", direction: "desc" }}
            defaultPageSize={10}
            searchFields={(r) => (isRestrictedIssue(r.issue) ? [] : [r.issue.cve, r.issue.name])}
            searchPlaceholder={t_i18n("Search CVE")}
            filtersInPill
            filters={[
              {
                key: "source",
                label: "Source",
                values: (r) => [isRestrictedIssue(r.issue) ? "restricted" : r.issue.source],
                options: [
                  { value: "opencti", label: "OpenCTI" },
                  { value: "openaev", label: "OpenAEV" },
                  { value: "restricted", label: t_i18n("Restricted") },
                ],
              },
            ]}
            exportName="ctem-case-issues"
            urlKey="i_"
            emptyLabel={t_i18n("No issue in this case")}
            rowTestId="ctem-case-issue-row"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t_i18n("History")}</CardTitle>
        </CardHeader>
        <CardContent>
          <CaseAuditTrail
            rows={activity}
            phases={kase.phases}
            currentPhase={kase.current_phase}
            personName={personName}
            actorName={actorNameOf}
            actionText={actionTextOf}
            changeText={changeTextOf}
            renderRun={renderRun}
            renderEvent={renderEvent}
          />
        </CardContent>
      </Card>

      <Dialog open={dialog !== null} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent size="md">
          <DialogHeader>
            <DialogTitle>{dialog ? dialogTitle[dialog] : ""}</DialogTitle>
            <DialogDescription>{kase.title}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            {dialog === "start" && <p className="content-base">{t_i18n("You become the owner if the case has none.")}</p>}
            {dialog === "split" && (
              <SettingsField
                id="case-split-title"
                label={t_i18n("Name of the new case")}
                helper={t_i18n("The new case starts in New and keeps these issues through every recompute.")}
                required
              >
                <Input
                  id="case-split-title"
                  aria-labelledby={settingsFieldLabelId("case-split-title")}
                  aria-describedby={settingsFieldHintId("case-split-title")}
                  maxLength={300}
                  value={splitTitle}
                  onChange={(e) => setSplitTitle(e.target.value)}
                />
              </SettingsField>
            )}
            {dialog === "resolve" && (
              <SettingsField id="case-evidence" label={t_i18n("Evidence")} helper={t_i18n("A ticket, a change or a validation run.")} required>
                <Textarea
                  id="case-evidence"
                  aria-labelledby={settingsFieldLabelId("case-evidence")}
                  aria-describedby={settingsFieldHintId("case-evidence")}
                  rows={3}
                  maxLength={1000}
                  value={evidence}
                  onChange={(e) => setEvidence(e.target.value)}
                />
              </SettingsField>
            )}
            {(dialog === "accept" || dialog === "reopen") && (
              <SettingsField id="case-reason" label={t_i18n("Reason")} required>
                <Textarea
                  id="case-reason"
                  aria-labelledby={settingsFieldLabelId("case-reason")}
                  rows={3}
                  maxLength={REASON_MAX}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </SettingsField>
            )}
            {dialog === "accept" && (
              <SettingsField id="case-expiry" label={t_i18n("Expires on")} helper={t_i18n("The case returns to New on that day.")} required>
                <DateFilterInput
                  id="case-expiry"
                  aria-labelledby={settingsFieldLabelId("case-expiry")}
                  aria-describedby={settingsFieldHintId("case-expiry")}
                  min={inDays(1)}
                  value={expiry}
                  onChange={setExpiry}
                />
              </SettingsField>
            )}
            {formError && <p className="content-compact text-feedback-error-primary" role="alert">{formError}</p>}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)}>
              {t_i18n("Cancel")}
            </Button>
            <Button
              onClick={confirm}
              disabled={
                update.isPending ||
                split.isPending ||
                (dialog === "split" && (!splitTitle.trim() || picked.length === 0)) ||
                (dialog === "resolve" && !evidence.trim()) ||
                ((dialog === "accept" || dialog === "reopen") && !reason.trim()) ||
                (dialog === "accept" && !expiry)
              }
            >
              {dialog ? dialogTitle[dialog] : ""}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
