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
 * - automations are on and wait for the next case event: who they are, and
 *   how many that could also work it are switched off.
 * An observer of the case events (`trigger_config.role`, #2054) is never named.
 * Calls to action are DS buttons; no inline link in the secondary colour.
 */
import { useState, type ComponentProps } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { CtemLoopSetupDialog } from "@/components/fleets/CtemLoopSetupDialog";
import { showsCtemLoopSetupEntry } from "@/components/fleets/CtemLoopSetupEntry";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { CaseAutomation, CaseWork } from "./case-workers";

interface CaseWorkCardProps {
  work: CaseWork;
  /** The CTEM loop fleet the reader sees, `null` when none. */
  loopFleetId: string | null;
  /** Start the case by hand; absent when the case is not waiting to start. */
  onStart?: () => void;
}

function workerLabel(a: Pick<CaseAutomation, "agentName" | "fleetName">, unknownAgent: string): string {
  const agent = a.agentName ?? unknownAgent;
  return a.fleetName ? `${agent} (${a.fleetName})` : agent;
}

export function CaseWorkCard({ work, loopFleetId, onStart }: CaseWorkCardProps) {
  if (work.kind === "unknown") return null;
  if (work.kind === "none") return <NothingWorksCase loopFleetId={loopFleetId} onStart={onStart} />;
  if (work.kind === "off") return <AutomationsOff automations={work.automations} loopFleetId={loopFleetId} onStart={onStart} />;
  return <WorkedBy work={work} />;
}

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

/** A run works the case, failed on it, or automations wait for the next case event. */
function WorkedBy({ work }: { work: Extract<CaseWork, { kind: "running" | "failed" | "on" }> }) {
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
  const more = work.kind === "on" ? work.automations.length - 1 : 0;
  const switchedOff = work.kind === "on" ? work.off : [];
  const queued = run && (run.status === "pending" || run.status === "queued");
  const state =
    work.kind === "failed"
      ? t_i18n("The last run failed: open it to read why.")
      : work.kind === "running"
        ? queued
          ? t_i18n("Queued since {since}", { since: intl.formatDate(run!.created_at, { dateStyle: "medium", timeStyle: "short" }) })
          : t_i18n("Running since {since}", { since: intl.formatDate(run!.created_at, { dateStyle: "medium", timeStyle: "short" }) })
        : t_i18n("Waits for the next case event.");
  return (
    <Card data-testid="ctem-case-work" data-state={work.kind}>
      <CardContent className="flex flex-col gap-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <span className="flex min-w-0 items-center gap-3">
            <Icon name="bot" size={20} className="shrink-0 text-default-secondary" />
            <span className="flex min-w-0 flex-col">
              <span className="truncate font-medium content-base">
                {t_i18n("Worked by {who}", { who })}
                {more > 0 && ` ${t_i18n("{count, plural, one {and # more} other {and # more}}", { count: more })}`}
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
        {switchedOff.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="content-compact font-medium text-default-secondary" data-testid="ctem-case-work-switched-off">
              {t_i18n("{count, plural, one {# automation that can work this case is switched off} other {# automations that can work this case are switched off}}", {
                count: switchedOff.length,
              })}
            </span>
            <SwitchOnRows automations={switchedOff} testId="ctem-case-work-switched-off-list" />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
