// SPDX-License-Identifier: Apache-2.0

/**
 * Who works this case (#1266), at the top of the case body. Read from live
 * state (`case-workers.ts`), never a generic sentence:
 * - nothing listens to the case events: one elevated empty state with three
 *   choices on equal footing (the built-in CTEM loop, your own fleet or agent,
 *   working it yourself), none presented as the default;
 * - automations exist but are switched off: each one named with its agent and
 *   fleet, and "Switch on";
 * - a run works the case, or failed: the agent, its fleet, the run state and
 *   the run;
 * - automations are on and no run works the case: who they are (the others
 *   named on hover), the gate the loop waits on with its decision, else the
 *   stage it waits on, and its last run with its outcome. Switched-off
 *   automations are never offered here: an enabled worker owns the case (Sam,
 *   25 Nov 2026); "Switch on" lives in the off state alone.
 * An observer of the case events (`trigger_config.role`, #2054) is never named.
 * Calls to action are DS buttons; no inline link in the secondary colour.
 */
import { useState, type ComponentProps } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { CtemLoopSetupDialog } from "@/components/fleets/CtemLoopSetupDialog";
import { showsCtemLoopSetupEntry } from "@/components/fleets/CtemLoopSetupEntry";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { CtemCaseRun, CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { timelineOutcome, type TimelineOutcome } from "@/lib/ctem-fleet-timeline";
import { workItemPath } from "@/lib/task-routes";
import { cn } from "@/lib/utils";
import { GATE_LABEL } from "./case-gates";
import type { CaseAutomation, CaseLoopWait, CaseWork } from "./case-workers";

interface CaseWorkCardProps {
  work: CaseWork;
  /** What the loop waits on, read from the case's gates and state (`caseLoopWait`). */
  wait?: CaseLoopWait;
  /** The CTEM loop fleet the reader sees, `null` when none. */
  loopFleetId: string | null;
  /** Start the case by hand; absent when the case is not waiting to start. */
  onStart?: () => void;
}

function workerLabel(a: Pick<CaseAutomation, "agentName" | "fleetName">, unknownAgent: string): string {
  const agent = a.agentName ?? unknownAgent;
  return a.fleetName ? `${agent} (${a.fleetName})` : agent;
}

export function CaseWorkCard({ work, wait = NO_WAIT, loopFleetId, onStart }: CaseWorkCardProps) {
  if (work.kind === "unknown") return null;
  if (work.kind === "none") return <NothingWorksCase loopFleetId={loopFleetId} onStart={onStart} />;
  if (work.kind === "off") return <AutomationsOff automations={work.automations} loopFleetId={loopFleetId} onStart={onStart} />;
  return <WorkedBy work={work} wait={wait} />;
}

const NO_WAIT: CaseLoopWait = { kind: "unknown" };

/** The three ways to work a case, on equal footing. */
function NothingWorksCase({ loopFleetId, onStart }: { loopFleetId: string | null; onStart?: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Card data-testid="ctem-case-work" data-state="none">
      <CardContent className="flex flex-col items-center gap-6 px-6 py-10 text-center">
        <div className="flex max-w-2xl flex-col items-center gap-2">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10 text-accent">
            <Icon name="workflow" size={24} />
          </span>
          <h2 className="title-sm">{t_i18n("No automation works this case yet")}</h2>
          <p className="content-base text-default-secondary">
            {t_i18n(
              "Working a case means reading how it would be exploited, an analyst review, validating the exposure, deciding the remediation and writing the treatment plan. You choose who does it.",
            )}
          </p>
        </div>
        <CaseWorkChoices loopFleetId={loopFleetId} onStart={onStart} />
      </CardContent>
    </Card>
  );
}

function CaseWorkChoices({ loopFleetId, onStart }: { loopFleetId: string | null; onStart?: () => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const canSetUp = showsCtemLoopSetupEntry(Boolean(isAdmin), Boolean(isXtmOne), Boolean(user?.impersonating));
  const [setupOpen, setSetupOpen] = useState(false);
  const [pickAgent, setPickAgent] = useState(false);

  return (
    <div className="grid w-full max-w-5xl grid-cols-1 gap-4 text-left md:grid-cols-3" data-testid="ctem-case-work-choices">
      <Choice
        icon="workflow"
        title={t_i18n("Use the built-in CTEM loop")}
        body={t_i18n("The seeded CTEM loop fleet works every case end to end, with your gates, once it is set up.")}
        testId="ctem-case-work-choice-loop"
      >
        {canSetUp ? (
          <Button variant="outline" className={CHOICE_BUTTON} onClick={() => setSetupOpen(true)}>
            {t_i18n("Set up the CTEM loop")}
          </Button>
        ) : (
          <Button
            variant="outline"
            className={CHOICE_BUTTON}
            onClick={() => navigate(loopFleetId ? `/fleets/${loopFleetId}` : "/fleets")}
          >
            {t_i18n("Open the CTEM loop fleet")}
          </Button>
        )}
      </Choice>
      <Choice
        icon="bot"
        title={t_i18n("Use your own fleet or agent")}
        body={t_i18n("Any automation that starts on the case events (case opened or reopened) and whose filter takes this case works it.")}
        testId="ctem-case-work-choice-own"
      >
        <Button variant="outline" className={CHOICE_BUTTON} onClick={() => setPickAgent(true)}>
          {t_i18n("Add a case automation")}
        </Button>
      </Choice>
      <Choice
        icon="user"
        title={t_i18n("Work it yourself")}
        body={t_i18n("Assign an owner and move the case by hand: start it, resolve it with evidence or accept the risk.")}
        testId="ctem-case-work-choice-self"
      >
        {onStart && (
          <Button variant="outline" className={`gap-1.5 ${CHOICE_BUTTON}`} onClick={onStart}>
            <Icon name="play" size={16} />
            {t_i18n("Start")}
          </Button>
        )}
      </Choice>
      {setupOpen && <CtemLoopSetupDialog open onOpenChange={setSetupOpen} />}
      {pickAgent && <CaseAutomationAgentDialog onClose={() => setPickAgent(false)} />}
    </div>
  );
}

/** A choice's button stays inside its box at every width and in every language: the label wraps instead of overflowing. */
const CHOICE_BUTTON = "h-auto min-h-8 max-w-full whitespace-normal text-left";

function Choice({
  icon,
  title,
  body,
  testId,
  children,
}: {
  icon: ComponentProps<typeof Icon>["name"];
  title: string;
  body: string;
  testId: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-sm border border-elevation-subtle-soft p-4" data-testid={testId}>
      <span className="flex items-center gap-2 font-medium content-base">
        <Icon name={icon} size={16} className="shrink-0 text-default-secondary" />
        {title}
      </span>
      <p className="flex-1 content-compact text-default-secondary">{body}</p>
      {children && <div className="min-w-0 pt-1">{children}</div>}
    </div>
  );
}

/** Pick the agent the case automation goes on; its page opens the assignment panel preset on the case events. */
function CaseAutomationAgentDialog({ onClose }: { onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: agents } = useAgents();
  const [agentId, setAgentId] = useState("");
  const options = (agents ?? []).map((a) => ({
    value: a.id,
    label: a.name,
    description: a.fleet?.fleet_name ?? undefined,
  }));
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t_i18n("Add a case automation")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Choose the agent that works cases. Its page opens a new assignment triggered by the case events.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <SearchableSelect
            aria-label={t_i18n("Agent")}
            options={options}
            value={agentId}
            onValueChange={setAgentId}
            placeholder={t_i18n("Select an agent")}
            className="w-full"
          />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button disabled={!agentId} onClick={() => navigate(`/agents/${agentId}?add-assignment=ctem-case`)}>
            {t_i18n("Continue")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The switched-off automations of a case, each by name with its Switch on button. */
function SwitchOnRows({ automations, testId }: { automations: CaseAutomation[]; testId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  // Switching an automation on is a gated assignment write: disabled with AI off.
  const aiOff = useAiActionDisabled();
  const unknownAgent = t_i18n("An agent you cannot see");
  return (
    <ul className="flex w-full max-w-3xl flex-col gap-2 text-left" data-testid={testId}>
      {automations.map((a) => (
        <li key={a.id} className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft px-4 py-3">
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-medium content-base">{a.name}</span>
            <span className="truncate content-compact text-default-secondary">{workerLabel(a, unknownAgent)}</span>
          </span>
          <AiDisabledTip>
            <Button
              variant="outline"
              size="sm"
              disabled={update.isPending || aiOff}
              onClick={() =>
                update.mutate(
                  { id: a.id, enabled: true },
                  {
                    onSuccess: () => toast({ title: t_i18n("Automation switched on"), variant: "success" }),
                    onError: (err) => toast({ title: err.message, variant: "error" }),
                  },
                )
              }
            >
              {t_i18n("Switch on")}
            </Button>
          </AiDisabledTip>
        </li>
      ))}
    </ul>
  );
}

/** Automations exist on the case events but every one is off. */
function AutomationsOff({ automations, loopFleetId, onStart }: { automations: CaseAutomation[]; loopFleetId: string | null; onStart?: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Card data-testid="ctem-case-work" data-state="off">
      <CardContent className="flex flex-col items-center gap-6 px-6 py-10 text-center">
        <div className="flex max-w-2xl flex-col items-center gap-2">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10 text-accent">
            <Icon name="workflow" size={24} />
          </span>
          <h2 className="title-sm">{t_i18n("The automations that can work this case are switched off")}</h2>
          <p className="content-base text-default-secondary">
            {t_i18n("{count, plural, one {# automation can work this case but is off.} other {# automations can work this case but are off.}}", {
              count: automations.length,
            })}
          </p>
        </div>
        <SwitchOnRows automations={automations} testId="ctem-case-work-off-list" />
        <CaseWorkChoices loopFleetId={loopFleetId} onStart={onStart} />
      </CardContent>
    </Card>
  );
}

/** The badge words of the last run, the keys the case history uses. @i18n-keys */
const LAST_RUN_OUTCOME_LABELS = {
  succeeded: "Run outcome: succeeded",
  failed: "Run outcome: failed",
  running: "Run outcome: running",
  pending: "Run outcome: pending",
  interrupted: "Run outcome: interrupted",
  cancelled: "Run outcome: cancelled",
} as const satisfies Record<TimelineOutcome, string>;

/** What the loop waits on at each stage a worked case can show. @i18n-keys */
const STAGE_WAIT_LABELS = {
  new: "Waits for the case to start",
  analyzing: "Waits for the analysis to finish",
  waiting_decision: "Waits for a decision",
  remediating: "Waits for the remediation to be applied",
  validating: "Waits for the validation to finish",
  mitigated_watching: "Watches whether the exposure comes back",
  resolved: "Waits for the next case event.",
  accepted_risk: "Waits for the next case event.",
} as const satisfies Record<CtemCaseState, string>;

/** The other enabled automations, named on hover and focus ("and N more"). */
function MoreWorkers({ others, unknownAgent }: { others: CaseAutomation[]; unknownAgent: string }) {
  const { t_i18n } = useFormatter();
  if (others.length === 0) return null;
  const names = others.map((a) => `${a.name}: ${workerLabel(a, unknownAgent)}`).join("\n");
  return (
    <>
      {" "}
      <Tip content={<span className="whitespace-pre-line">{names}</span>}>
        <span tabIndex={0} className={cn("underline decoration-dotted underline-offset-2", TIP_WRAP_FOCUS_RING)} data-testid="ctem-case-work-more">
          {t_i18n("{count, plural, one {and # more} other {and # more}}", { count: others.length })}
        </span>
      </Tip>
    </>
  );
}

/** One row of the loop state: the gate and the last run share this box, so their text and actions sit on one grid. */
function LoopRow({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft px-4 py-3"
      data-testid={testId}
      data-loop-row=""
    >
      {children}
    </div>
  );
}

/** The loop's own state on the case while no run works it: the gate it waits on, else its stage, and its last run. */
function LoopState({ wait, last, unknownAgent }: { wait: CaseLoopWait; last: CtemCaseRun | null; unknownAgent: string }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const outcome = last ? timelineOutcome(last.outcome, last.status) : null;
  const ended = last ? (last.completed_at ?? last.created_at) : null;
  return (
    <div className="flex flex-col gap-2" data-testid="ctem-case-work-loop">
      {wait.kind === "gate" && (
        <LoopRow testId="ctem-case-work-gate">
          <span className="flex min-w-0 flex-col">
            <span className="content-compact text-default-secondary">{t_i18n("Waits for a decision")}</span>
            <span className="font-medium content-base">{t_i18n(GATE_LABEL[wait.gate] ?? "A decision")}</span>
          </span>
          {wait.workItemId && (
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => navigate(workItemPath(wait.workItemId!))}>
              <Icon name="external-link" size={16} />
              {t_i18n("Open the decision")}
            </Button>
          )}
        </LoopRow>
      )}
      {last && (
        <LoopRow testId="ctem-case-work-last-run">
          <span className="flex min-w-0 flex-wrap items-center gap-2 content-compact text-default-secondary">
            {t_i18n("Last run by {agent}, {when}", {
              agent: last.agent_name ?? unknownAgent,
              when: intl.formatDate(ended!, { dateStyle: "medium", timeStyle: "short" }),
            })}
            {outcome && (
              <Badge variant={outcome === "succeeded" ? "success" : outcome === "interrupted" ? "warning" : "secondary"} className="shrink-0">
                {t_i18n(LAST_RUN_OUTCOME_LABELS[outcome])}
              </Badge>
            )}
          </span>
          <Button variant="outline" size="sm" className="gap-1.5" onClick={() => navigate(`/operations/executions/${last.id}`)}>
            <Icon name="external-link" size={16} />
            {t_i18n("Open the run")}
          </Button>
        </LoopRow>
      )}
    </div>
  );
}

/** A run works the case, failed on it, or the loop owns it between runs: what it did and what it waits on. */
function WorkedBy({ work, wait }: { work: Extract<CaseWork, { kind: "running" | "failed" | "on" }>; wait: CaseLoopWait }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const unknownAgent = t_i18n("An agent you cannot see");
  const run = work.kind === "on" ? null : work.run;
  const runAutomation = run ? work.automations.find((a) => a.agentId === run.agent_id) : undefined;
  const who = run
    ? workerLabel({ agentName: run.agent_name, fleetName: runAutomation?.fleetName ?? null }, unknownAgent)
    : work.automations.length > 0
      ? workerLabel(work.automations[0], unknownAgent)
      : unknownAgent;
  const others = work.kind === "on" ? work.automations.slice(1) : [];
  const queued = run && (run.status === "pending" || run.status === "queued");
  const state =
    work.kind === "failed"
      ? t_i18n("The last run failed: open it to read why.")
      : work.kind === "running"
        ? queued
          ? t_i18n("Queued since {since}", { since: intl.formatDate(run!.created_at, { dateStyle: "medium", timeStyle: "short" }) })
          : t_i18n("Running since {since}", { since: intl.formatDate(run!.created_at, { dateStyle: "medium", timeStyle: "short" }) })
        : wait.kind === "gate"
          ? t_i18n("Waits for a person")
          : wait.kind === "stage"
            ? t_i18n(STAGE_WAIT_LABELS[wait.state])
            : t_i18n("Waits for the next case event.");
  return (
    <Card data-testid="ctem-case-work" data-state={work.kind}>
      <CardContent className="flex flex-col gap-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <span className="flex min-w-0 items-center gap-3">
            <Icon name="bot" size={20} className="shrink-0 text-default-secondary" />
            <span className="flex min-w-0 flex-col">
              <span className="font-medium content-base">
                {t_i18n("Worked by {who}", { who })}
                <MoreWorkers others={others} unknownAgent={unknownAgent} />
              </span>
              <span
                className={work.kind === "failed" ? "content-compact text-feedback-error-primary" : "content-compact text-default-secondary"}
                data-testid="ctem-case-work-state"
              >
                {state}
              </span>
            </span>
          </span>
          {run && (
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => navigate(`/operations/executions/${run.id}`)}>
              <Icon name="external-link" size={16} />
              {t_i18n("Open the run")}
            </Button>
          )}
        </div>
        {work.kind === "on" && (wait.kind === "gate" || work.last) && <LoopState wait={wait} last={work.last} unknownAgent={unknownAgent} />}
      </CardContent>
    </Card>
  );
}
