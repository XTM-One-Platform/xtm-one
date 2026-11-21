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
import { useNavigate, useParams } from "react-router-dom";
import { Icon, Textarea } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NotFoundState } from "@/components/ui/not-found-state";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { PageSkeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import { useToast } from "@/components/ui/toast";
import { Input } from "@/components/ui/input";
import { useMe, useUserDirectoryByIds } from "@/hooks/api/useAuthApi";
import {
  isRestrictedIssue,
  useCtemCase,
  useSplitCtemCase,
  useUpdateCtemCase,
  type CtemCaseIssue,
  type CtemCaseUpdate,
} from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { isNotFoundError } from "@/lib/api";
import { exactCount, formatCount } from "@/lib/number-format";
import { useNow } from "@/hooks/useTick";
import { CaseLoopLine, CaseUrgencyChip, RetestChip, hasRetestData, retestRank } from "./ctem/case-loop";
import { CaseExploitation } from "./ctem/CaseExploitation";
import { CaseOpenedBy } from "./ctem/CaseOpenedBy";
import { CASE_STATE_LABEL, CaseOwner, CaseStateChip } from "./ctem/cases-shared";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { LoopTraceDialog } from "./ctem/loop/LoopTraceDialog";

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

/** History lines, English, translated where drawn. */
const EVENT_LABEL: Record<string, string> = {
  created: "Created the case",
  issue_added: "Added an issue",
  issue_closed: "Closed an issue",
  state: "Changed the state to",
  owner: "Changed the owner",
  due: "Changed the due date",
  note: "Added a note",
  split: "Split an issue out",
  merged: "Merged cases",
  // The store writes `merge` (ctem_cases_store.merge_cases, S5c).
  merge: "Merged cases",
  // The loop (CTEM-03, #857; shown by CTEM-03-UI, #892).
  signal: "A signal joined the case",
  validation: "Recorded a validation",
  sla_breached: "Escalated: SLA breached",
  // How the CVE would be exploited, read from OpenCRQ by triage (CTEM-24-UI, #936).
  exploitation: "Recorded how it would be exploited",
};

/** The result word of a validation history row, translated where drawn. */
const VALIDATION_RESULT_LABEL: Record<string, string> = {
  passed: "Passed",
  not_exposed: "Not exposed",
  failed: "Failed",
  pending: "Pending",
};

type DialogKind = "start" | "resolve" | "accept" | "reopen" | "split" | null;

export default function CtemCaseDetail() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { data: me } = useMe();
  const { data: kase, isLoading, error, isError, refetch } = useCtemCase(id);
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
    for (const e of kase?.history ?? []) if (/^[0-9a-f-]{36}$/i.test(e.actor)) ids.add(e.actor);
    return [...ids];
  }, [kase]);
  const { data: people } = useUserDirectoryByIds(actorIds);
  const personName = (personId: string | null | undefined) =>
    personId ? (people?.find((p) => p.id === personId)?.full_name ?? null) : null;

  const issueRows: IssueRow[] = useMemo(
    () => (kase?.issues ?? []).map((issue, i) => ({ key: isRestrictedIssue(issue) ? `restricted-${i}` : `${issue.source}:${issue.external_id}`, issue })),
    [kase],
  );

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
          <span className="block max-w-[24rem] truncate font-medium" title={r.issue.name ?? r.issue.external_id}>
            {r.issue.cve ?? r.issue.name ?? r.issue.external_id}
          </span>
        ),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? "~" : (r.issue.cve ?? r.issue.name ?? "")),
      csv: (r) => (isRestrictedIssue(r.issue) ? t_i18n("Restricted") : (r.issue.cve ?? r.issue.name ?? "")),
    },
    {
      key: "source",
      header: "Source",
      cell: (r) =>
        isRestrictedIssue(r.issue)
          ? null
          : r.issue.source === "signal"
            ? t_i18n("Signal")
            : r.issue.source === "openaev"
              ? "OpenAEV"
              : "OpenCTI",
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
      key: "state",
      header: "State",
      cell: (r) => (isRestrictedIssue(r.issue) ? null : r.issue.open ? t_i18n("Open") : t_i18n("Closed")),
      sortValue: (r) => (isRestrictedIssue(r.issue) ? 2 : r.issue.open ? 0 : 1),
      csv: (r) => (isRestrictedIssue(r.issue) ? "" : r.issue.open ? "open" : "closed"),
    },
  ];

  // The next state as the filled primary, the platform order (secondary first).
  const primary =
    kase.state === "needs_attention"
      ? { label: t_i18n("Start"), kind: "start" as const }
      : kase.state === "in_progress"
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
    else if (dialog === "start") submit({ state: "in_progress", owner_user_id: kase.owner_user_id ?? me?.id });
    else if (dialog === "resolve") submit({ state: "resolved", evidence: evidence.trim() });
    else if (dialog === "reopen") submit({ state: "needs_attention", reason: reason.trim() });
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
      {/* The one CTEM header band (#11660): state, priority and owner on the
          breadcrumb line, the title, the actions on the title line. */}
      <CtemPageHeader
        back={{ to: "/ctem/cases", label: "Back to Cases" }}
        crumbExtras={
          <>
            <CaseStateChip state={kase.state} />
            <CaseUrgencyChip severity={kase.severity} priority={kase.priority} />
            <CaseOwner name={ownerName} />
            <CaseLoopLine c={kase} now={now} />
          </>
        }
        title={kase.title}
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

      {kase.details_unavailable.length > 0 && (
        <p className="content-compact text-default-secondary">
          {t_i18n("{products} did not answer: details of its issues are unavailable.", { products: kase.details_unavailable.join(", ") })}
        </p>
      )}

      <CaseOpenedBy caseId={kase.id} />

      <CaseExploitation entries={kase.exploitation} />

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
          <ol className="space-y-3" data-testid="ctem-case-history">
            {[...kase.history].reverse().map((e, i) => (
              <li key={`${e.at}-${i}`} className="flex flex-wrap items-baseline gap-x-2 content-base">
                <span className="font-medium">
                  {e.actor === "system" ? t_i18n("System") : (personName(e.actor) ?? t_i18n("Agent"))}
                </span>
                <span>{t_i18n(EVENT_LABEL[e.kind] ?? "Changed the case")}</span>
                {e.issue === "Restricted" && <span className="text-default-secondary">{t_i18n("Restricted")}</span>}
                {e.to && e.kind === "state" && <span>{t_i18n(CASE_STATE_LABEL[e.to as keyof typeof CASE_STATE_LABEL] ?? e.to)}</span>}
                {e.to && e.kind === "validation" && <span>{t_i18n(VALIDATION_RESULT_LABEL[e.to] ?? e.to)}</span>}
                {e.reason && <span className="text-default-secondary">{e.reason}</span>}
                {e.evidence && <span className="text-default-secondary">{e.evidence}</span>}
                {e.at && (
                  <span className="ml-auto whitespace-nowrap text-default-secondary">
                    {intl.formatDate(e.at, { dateStyle: "medium", timeStyle: "short" })}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      {/* The loop trace of a signal of "Opened by" (`?loop=signal:<id>`). */}
      <LoopTraceDialog />

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
                helper={t_i18n("The new case starts in Needs attention and keeps these issues through every recompute.")}
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
              <SettingsField id="case-expiry" label={t_i18n("Expires on")} helper={t_i18n("The case returns to Needs attention on that day.")} required>
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
