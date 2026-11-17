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

import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Plus, Bot, MessageSquare, Sparkles,
  Zap, Power, PowerOff, Upload, Loader2, X,
  Cog, Terminal, BookOpen, Wrench, Cpu,
  Radio, Globe, CheckCircle2, CircleSlash, Boxes,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { filterPillClass } from "@/components/ui/filter-pill";
import { Button } from "@/components/ui/button";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { PropertyFilter, type PropertyFilterOption } from "@/components/ui/property-filter";
import { useBuiltinCompanyTip, useBuiltinPropertyFilter } from "@/components/ui/builtin-fold-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { FavoriteStar } from "@/components/ui/favorite-star";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { useAgents, useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useActiveAgentIds } from "@/hooks/api/useAssignmentApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { useFavoriteAgents } from "@/hooks/useFavoriteAgents";
import { useAuth } from "@/hooks/useAuth";
import { canManageEntity, cn, formatCompactNumber, isEntityShared } from "@/lib/utils";
import { RoiListCell } from "@/components/roi/RoiListCell";
import { useRoiSummary, type RoiSummaryByIds } from "@/hooks/api/useRoiReportApi";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { ROI_COLUMN_HEADERS, roiColumnMetric, summarySortValue } from "@/lib/roi-summary";
import { generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { ICON_MAP } from "@/lib/constants";
import { CompanyManagedBadge, CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { IconMark } from "@/components/ui/icon-mark";
import { Tip } from "@/components/ui/tooltip";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { effectiveApproval, PERSONAL_APPROVALS_PATH } from "@/lib/hitl";
import { SharedBadge, SharedIcon } from "@/components/ui/shared-badge";
import { useAgenticAIEnabled, useChatEnabled } from "@/hooks/usePlatformConfig";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useFormatter } from "@/i18n";
import type { EntityProperty } from "@/hooks/useEntityFilter";
import type { Agent } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useRoutingModelLabel } from "@/hooks/useRoutingModelLabel";
import { Spinner } from "@filigran/design-system";

/**
 * Deployment properties surfaced as card/row indicators and offered as `has:`
 * filters in the search bar. One definition drives both, so an indicator and
 * its filter can never drift apart.
 *
 * Adding a property (e.g. `has:knowledge-base`) is a single entry here —
 * declared at module scope so `useEntityFilter` sees a stable identity.
 */
type AgentDeploymentProperty = EntityProperty<Agent> &
  PropertyFilterOption & {
    /** Tooltip on the indicator. */
    indicatorTip: string;
    /** Amber-toned indicator — the agent is reachable from outside the platform. */
    warning?: boolean;
  };

const AGENT_PROPERTIES: AgentDeploymentProperty[] = [
  {
    key: "channels-bots",
    label: "Channels & Bots",
    icon: Radio,
    description: "At least one messaging channel is configured",
    indicatorTip: "Channels & Bots configured",
    matches: (agent) => Object.keys(agent.channel_config || {}).length > 0,
  },
  {
    key: "web-exposure",
    label: "Web Exposure",
    icon: Globe,
    description: "Reachable through the public embed widget",
    indicatorTip: "Web Exposure enabled: this agent is publicly accessible",
    warning: true,
    matches: (agent) => agent.web_enabled,
  },
];

/** The built-in fold of the list (#11831): the API's `is_builtin`, never the tag; module scope for a stable identity. */
const AGENT_BUILTIN_FOLD = { list: "agents", isBuiltin: (agent: Agent) => agent.is_builtin };

// ---------------------------------------------------------------------------
// Shared cells — used by both the card grid and the list view so the two
// surfaces can never drift apart on status, model, counts, or actions.
// ---------------------------------------------------------------------------

/** Deployment indicator icons — rendered only for the properties an agent has. */
function DeploymentIndicators({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const active = AGENT_PROPERTIES.filter((p) => p.matches(agent));
  if (active.length === 0) return null;
  return (
    <>
      {active.map(({ key, icon: Icon, indicatorTip, warning }) => (
        <IconMark key={key} label={t_i18n(indicatorTip)}>
          <Icon
            className={cn("h-3.5 w-3.5 shrink-0", warning ? "text-feedback-warning-primary" : "text-default-secondary")}
            aria-hidden="true"
          />
        </IconMark>
      ))}
    </>
  );
}

/**
 * Whether this agent's tool calls pause for approval (FR-046).
 *
 * Green for gated, amber for unattended — the reading the profile card and the
 * integration page already carry, where amber marks the permissive state
 * because that is the one an operator needs to notice. It answers a question
 * the configuration tab could only answer one agent at a time.
 *
 * The agent's `approve_tool_calls`, else the install default. The platform
 * switch outranks both, which the notice above the list says once.
 */
/**
 * Whether the gate is suspended for the person reading, and why.
 *
 * The agent's switch describes the AGENT, so it stays what the icon and the
 * recap count report: two admins must not see different states for the same
 * agent, and the count is what an operator uses to decide whether to change
 * them. What is per-reader is said once, above the list — the same division the
 * tools page makes, for the same reason.
 */
function useGateSuspended() {
  const { data: mine } = useToolApprovals();
  return {
    platformOff: mine?.tool_approvals_enabled === false,
    allowAll: mine?.approve_all_tools ?? false,
    installDefault: mine?.approve_tool_calls_default,
  };
}

/** Whether this agent asks before running tools: its own setting, else the
 *  platform default. A disabled agent runs nothing, so it asks nothing, the
 *  rule the backend's approval usage counts by. */
function asksBeforeRunningTools(agent: Agent, installDefault: boolean | undefined): boolean {
  return agent.enabled && effectiveApproval(agent.approve_tool_calls, installDefault) === true;
}

function AgentGateIcon({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const { platformOff, allowAll, installDefault } = useGateSuspended();
  /* Only on an agent that asks: approvals are opt-in, so an agent that runs
     its tools without asking is the ordinary case and carries no mark. The
     tooltip carries the reader's own override too; the banner above the list
     says it once for all of them. */
  if (!asksBeforeRunningTools(agent, installDefault)) return null;
  const label = platformOff
    ? t_i18n("Set to ask before running tools, but approvals are suspended for the whole platform right now")
    : allowAll
    ? t_i18n("Set to ask before running tools, but you allow every tool, so it does not ask you")
    : t_i18n("Asks before running tools");
  return (
    <IconMark label={label}>
      <HitlIcon state="gated" decorative />
    </IconMark>
  );
}

/**
 * How many of these agents gate, and the way out of it (FR-046).
 *
 * The count is the question an operator actually arrives with — "why is
 * everything asking me?" — and answering it agent by agent is what the
 * configuration tab already made possible and nobody does. So it opens the
 * list, and links to the one control that answers the question wholesale: the
 * user's own allow-all, which the profile owns.
 *
 * Counts the loaded, filtered set rather than the fleet: this sits beside the
 * filters, so a number that ignored them would describe a list nobody is
 * looking at.
 */
function GatedAgentsRecap({ agents }: { agents: Agent[] }) {
  const { t_i18n } = useFormatter();
  const { platformOff, allowAll, installDefault } = useGateSuspended();
  const [open, setOpen] = useState(false);
  const gated = useMemo(
    () => agents.filter((a) => asksBeforeRunningTools(a, installDefault)),
    [agents, installDefault],
  );

  if (gated.length === 0) return null;

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1.5 px-2 text-[11px] text-default-secondary hover:text-default-primary"
        onClick={() => setOpen(true)}
      >
        <HitlIcon state="gated" decorative />
        {/* While approvals are suspended nothing asks: the count is what the
            agents are set to, as the notice above the list says. */}
        {platformOff
          ? t_i18n("{n, plural, one {# agent set to ask} other {# agents set to ask}}", { n: gated.length })
          : t_i18n("{n, plural, one {# agent asks first} other {# agents ask first}}", { n: gated.length })}
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <HitlIcon state="gated" className="size-4" decorative />
              {t_i18n("Agents that ask before running tools")}
            </DialogTitle>
            <DialogDescription>
              {t_i18n(
                "Before a tool call that is not known to be read-only, they wait for a person to approve it, in chat and on their scheduled runs alike.",
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="max-h-72 space-y-1 overflow-y-auto">
            {gated.map((agent) => (
              <Link
                key={agent.id}
                to={`/agents/${agent.id}`}
                className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 hover:bg-elevation-highlight"
                onClick={() => setOpen(false)}
              >
                <HitlIcon state="gated" className="size-3" />
                <span className="min-w-0 flex-1 truncate content-compact">{agent.name}</span>
              </Link>
            ))}
          </div>

          {/* The wholesale answer, not a per-agent one: turning an agent's
              switch off changes it for everybody, while the profile control
              changes it only for the person asking.

              Offered only while it is still an answer. Pointing somebody at a
              switch they have already flipped is what made "I allowed all
              tools and every agent still says it gates" read as a bug. */}
          {platformOff ? (
            <p className="text-[11px] text-default-secondary">
              {t_i18n(
                "An administrator has suspended approvals for the whole platform, so none of these asks anyone right now.",
              )}
            </p>
          ) : allowAll ? (
            <p className="text-[11px] text-default-secondary">
              {t_i18n(
                "You allow every tool, so none of these asks you. They still ask everyone else first.",
              )}
            </p>
          ) : (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("To stop being asked about a tool, or about any tool, without changing these agents,")}{" "}
              <Link
                to={PERSONAL_APPROVALS_PATH}
                className="text-accent underline-offset-2 hover:underline"
                onClick={() => setOpen(false)}
              >
                {t_i18n("manage your approvals in your profile")}
              </Link>
              .
            </p>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function AgentStatusBadge({ agent, isRunning }: { agent: Agent; isRunning?: boolean }) {
  const { t_i18n } = useFormatter();
  if (isRunning && agent.enabled) {
    return (
      <Badge variant="cyan" className="content-caption gap-1">
        <Spinner size="sm" tone="inherit" />
        {t_i18n("Running")}
      </Badge>
    );
  }
  return (
    <Badge variant={agent.enabled ? "success" : "outline"} className="content-caption">
      {agent.enabled ? t_i18n("Active") : t_i18n("Disabled")}
    </Badge>
  );
}

/**
 * Icon-only status — the card variant. Cards trade the badge's wording for the
 * horizontal room the agent name needs; the tooltip keeps the label reachable.
 */
function AgentStatusIcon({ agent, isRunning }: { agent: Agent; isRunning?: boolean }) {
  const { t_i18n } = useFormatter();
  const { Icon, className, label } = isRunning && agent.enabled
    ? { Icon: Loader2, className: "text-accent animate-spin", label: t_i18n("Running") }
    : agent.enabled
      ? { Icon: CheckCircle2, className: "text-feedback-success-primary", label: t_i18n("Active") }
      : { Icon: CircleSlash, className: "text-default-secondary", label: t_i18n("Disabled") };
  return (
    <IconMark label={label}>
      <Icon className={cn("h-3.5 w-3.5 shrink-0", className)} aria-hidden="true" />
    </IconMark>
  );
}

/**
 * Which LLM the agent runs, at a glance: the pinned model, the platform default
 * when there is no override, or "No LLM" for agents that never call one.
 */
function AgentModelLabel({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const routingModel = useRoutingModelLabel();
  const onRouting = !agent.skip_llm && agent.llm_model_source === "routing";
  const modelLabel = agent.skip_llm
    ? t_i18n("No LLM")
    : onRouting
      ? t_i18n("Routing model")
      : (agent.llm_model || t_i18n("Default"));
  const modelTip = agent.skip_llm
    ? t_i18n("This agent does not call an LLM")
    : onRouting
      ? routingModel.label
      : agent.llm_model
        ? t_i18n("Model: {model}", { model: agent.llm_model })
        : t_i18n("Uses the platform default model");
  const modelIsMuted = agent.skip_llm || (!agent.llm_model && !onRouting);

  return (
    <Tip content={modelTip}>
      <span className={cn(
        "flex items-center gap-1 content-caption min-w-0 max-w-[150px]",
        modelIsMuted ? "text-default-secondary italic" : "text-default-secondary",
      )}>
        <Cpu className="h-3 w-3 shrink-0" />
        <span className="truncate">{modelLabel}</span>
      </span>
    </Tip>
  );
}

/** Integration / MCP / KB / custom-tool / assignment counts. */
function AgentResourceCounts({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const integrationCount = Object.values(agent.integration_bindings || {}).flat().length;
  return (
    <div className="flex items-center gap-2 text-default-secondary shrink-0">
      <Tip content={t_i18n("{count, plural, one {# integration} other {# integrations}}", { count: integrationCount })}>
        <span className="flex items-center gap-0.5 content-caption"><Cog className="h-3 w-3" />{integrationCount}</span>
      </Tip>
      <Tip content={t_i18n("{count, plural, one {# MCP server} other {# MCP servers}}", { count: (agent.mcp_server_ids || []).length })}>
        <span className="flex items-center gap-0.5 content-caption"><Terminal className="h-3 w-3" />{(agent.mcp_server_ids || []).length}</span>
      </Tip>
      <Tip content={t_i18n("{count, plural, one {# knowledge base} other {# knowledge bases}}", { count: (agent.knowledge_base_ids || []).length })}>
        <span className="flex items-center gap-0.5 content-caption"><BookOpen className="h-3 w-3" />{(agent.knowledge_base_ids || []).length}</span>
      </Tip>
      <Tip content={t_i18n("{count, plural, one {# custom tool} other {# custom tools}}", { count: (agent.custom_tool_ids || []).length })}>
        <span className="flex items-center gap-0.5 content-caption"><Wrench className="h-3 w-3" />{(agent.custom_tool_ids || []).length}</span>
      </Tip>
      {/* Shown at zero like the four above it. Hiding it there made an agent
          with no assignments look like one where assignments do not apply,
          which is the opposite of what an empty count means. */}
      <Tip content={t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: agent.assignment_count })}>
        <span className="flex items-center gap-0.5 content-caption"><Zap className="h-3 w-3" />{formatCompactNumber(agent.assignment_count)}</span>
      </Tip>
    </div>
  );
}

/** Chat + enable/disable actions. Callers are responsible for swallowing the click.
 *  The power button is icon-only, so its accessible name states the action it
 *  takes on this agent ("Disable agent" while enabled), like the agent page. */
export function AgentActions({ agent, isAdmin, userId, chatEnabled, isLinkedGA }: { agent: Agent; isAdmin: boolean; userId?: string; chatEnabled: boolean; isLinkedGA: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const update = useUpdateAgent();
  // canManage = full edit rights. canToggleEnabled = can at least enable/disable (admins always can).
  const canManage = canManageEntity(agent, userId, isAdmin);
  const canToggleEnabled = canManage || isAdmin;

  const aiOff = useAiActionDisabled();
  /* Starting a NEW chat needs AI; opening an existing conversation does
     not, which is why only the ``?agent=`` entry points are gated. */
  const chatDisabled = !agent.enabled || agent.disable_chat || aiOff;
  const chatTip = aiOff
    ? t_i18n("AI is disabled on this platform, so this agent cannot answer.")
    : agent.disable_chat
      ? t_i18n("Chat disabled for this agent")
      : !agent.enabled ? t_i18n("Agent is disabled") : t_i18n("Chat");

  return (
    <>
      {chatEnabled && (
        <Tip content={chatTip}>
          <Button aria-label={chatTip}
            variant="ghost"
            size="sm"
            className={cn("h-7 px-2 content-compact gap-1", chatDisabled ? "text-default-secondary opacity-50 cursor-not-allowed" : "text-accent hover:text-accent hover:bg-accent/10")}
            onClick={() => { if (!chatDisabled) navigate(`/chat?agent=${agent.id}`); }}
          >
            <MessageSquare className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      )}
      {canToggleEnabled && !isLinkedGA && (
        <Tip content={agent.enabled ? t_i18n("Disable agent") : t_i18n("Enable agent")}>
          <Button aria-label={agent.enabled ? t_i18n("Disable agent") : t_i18n("Enable agent")} variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => update.mutate({ id: agent.id, enabled: !agent.enabled })} disabled={aiOff}>
            {agent.enabled ? <Power className="h-4 w-4" /> : <PowerOff className="h-4 w-4" />}
          </Button>
        </Tip>
      )}
    </>
  );
}

export function AgentCard({ agent, isAdmin, userId, isRunning, chatEnabled = true, isLinkedGA = false, isFavorite, onToggleFavorite }: { agent: Agent; isAdmin: boolean; userId?: string; isRunning?: boolean; chatEnabled?: boolean; isLinkedGA?: boolean; isFavorite: boolean; onToggleFavorite: () => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const Icon = ICON_MAP[agent.icon] ?? Bot;
  const isShared = isEntityShared(agent, userId);

  const href = `/agents/${agent.id}`;

  return (
    <Card
      data-testid="agent-card"
      className={cn(
        "transition-all h-full cursor-pointer hover:bg-elevation-hover",
        agent.enabled ? "border-elevation-subtle" : "border-elevation-subtle-soft opacity-60",
      )}
      onClick={() => navigate(href)}
    >
      <CardContent className="p-4 flex flex-col h-full">
        <div className="flex items-center gap-3">
          {/* Only the identity cluster is the anchor — the same split
              `EntityTable` makes for rows. The card's own onClick keeps the
              whole surface clickable, while the star, the badges and the row
              actions stay OUTSIDE the <a>: a <button> nested in an anchor is
              invalid markup and gives unreliable keyboard/AT semantics, which
              swallowing the click event does not fix. */}
          <Link to={href} className="group/agent-card-link flex items-center gap-3 min-w-0 flex-1" onClick={(e) => e.stopPropagation()}>
            <div className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-sm",
              agent.enabled ? "bg-accent/10" : "bg-elevation-hover",
            )}>
              <Icon className={cn("h-5 w-5", agent.enabled ? "text-accent" : "text-default-secondary")} />
            </div>
            <div className="min-w-0 flex-1">
              {/* The name wraps to two lines instead of one ellipsis: at 1024 the
                  three-column grid leaves the title about 130 px, which cut most
                  built-in agent names. The grid row stretches every card to the
                  tallest one, so a two-line name never makes a row uneven. A
                  longer name still clamps; the full name stays on the title for
                  hover and unfolds when the card link has keyboard focus. */}
              <p
                data-testid="agent-card-name"
                title={agent.name}
                className="content-base font-medium line-clamp-2 break-words group-focus-visible/agent-card-link:line-clamp-none"
              >
                {agent.name}
              </p>
              <p className="content-caption text-default-secondary line-clamp-1">{agent.description || "\u00A0"}</p>
            </div>
          </Link>
          <div className="flex items-center gap-2 shrink-0">
            <FavoriteStar active={isFavorite} onToggle={onToggleFavorite} />
            {isLinkedGA && (
              <IconMark label={t_i18n("General Assistant")}>
                <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" aria-hidden="true" />
              </IconMark>
            )}
            <DeploymentIndicators agent={agent} />
          </div>
        </div>

        {/* Tags — fixed-height row for consistent card sizing. Sharing, status
            and company-management ride along on the right as icons rather than
            badges, so the row above keeps its full width for the agent name. */}
        <div className="flex items-center gap-1 mt-3 h-[18px]">
          <TagList tags={agent.tags} className="min-w-0 overflow-hidden" />
          <div className="flex items-center gap-1.5 ml-auto shrink-0">
            {isShared && <SharedIcon ownerName={agent.owner_name} />}
            {agent.is_company_managed && <CompanyManagedIcon label="Company-managed agent" />}
            <AgentGateIcon agent={agent} />
            <AgentStatusIcon agent={agent} isRunning={isRunning} />
          </div>
        </div>

        {/* Metadata — pushed to bottom */}
        <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
          <AgentModelLabel agent={agent} />
          <AgentResourceCounts agent={agent} />
          <div className="flex items-center gap-1 ml-auto shrink-0" onClick={(e) => { e.preventDefault(); e.stopPropagation(); }}>
            <AgentActions agent={agent} isAdmin={isAdmin} userId={userId} chatEnabled={chatEnabled} isLinkedGA={isLinkedGA} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * List-view columns — status, model, assignment/resource counts and the same
 * row actions the card offers.
 */
function useAgentColumns({
  isAdmin,
  userId,
  activeAgentIds,
  chatEnabled,
  linkedGAId,
  isFavorite,
  onToggleFavorite,
  roi,
}: {
  isAdmin: boolean;
  userId?: string;
  activeAgentIds: Set<string>;
  chatEnabled: boolean;
  linkedGAId: string | null;
  isFavorite: (agentId: string) => boolean;
  onToggleFavorite: (agentId: string) => void;
  /** The page's one `GET /roi/summary` read; `null` on Community Edition (no column). */
  roi: RoiSummaryByIds | null;
}): EntityTableColumn<Agent>[] {
  const { t_i18n } = useFormatter();
  return useMemo(() => [
    {
      // Headerless leading column: the star is the row's own control, not a
      // sortable dimension — favorites are pinned on top via `pinFirst`.
      key: "favorite",
      header: "",
      interactive: true,
      className: "w-8 pr-0",
      cell: (agent) => (
        <FavoriteStar active={isFavorite(agent.id)} onToggle={() => onToggleFavorite(agent.id)} />
      ),
    },
    {
      key: "name",
      header: "Name",
      sortValue: (agent) => agent.name,
      cell: (agent) => {
        const Icon = ICON_MAP[agent.icon] ?? Bot;
        return (
          <EntityNameCell
            icon={<Icon className={cn("h-4 w-4", agent.enabled ? "text-accent" : "text-default-secondary")} />}
            name={agent.name}
            description={agent.description || null}
            muted={!agent.enabled}
            // Up to six badges and indicators per row: under the name, or
            // they take its width and push the table past its card.
            badgesPlacement="below"
            // An array, not a fragment: a fragment always counts as one badge,
            // so a row with none drew an empty badge line under its text and
            // the name sat above the row's centre (#10746).
            badges={[
              agent.is_company_managed && <CompanyManagedBadge key="company" />,
              isEntityShared(agent, userId) && <SharedBadge key="shared" ownerName={agent.owner_name} />,
              linkedGAId === agent.id && (
                <IconMark key="general-assistant" label={t_i18n("General Assistant")}>
                  <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" aria-hidden="true" />
                </IconMark>
              ),
              AGENT_PROPERTIES.some((p) => p.matches(agent)) && <DeploymentIndicators key="deployment" agent={agent} />,
            ]}
          />
        );
      },
    },
    {
      key: "status",
      header: "Status",
      className: "whitespace-nowrap",
      // Running agents sort above merely-enabled ones, disabled ones last.
      sortValue: (agent) => agent.enabled ? (activeAgentIds.has(agent.id) ? 0 : 1) : 2,
      cell: (agent) => (
        <span className="flex items-center gap-1.5">
          <AgentStatusBadge agent={agent} isRunning={activeAgentIds.has(agent.id)} />
          <AgentGateIcon agent={agent} />
        </span>
      ),
    },
    {
      key: "model",
      header: "Model",
      // Model shows from a 72rem table (a 1440 px window) and Resources from
      // 56rem (1280 px): the table with Model was 1121 px in a 1034 px card.
      // hideBelow, not a page class, so the Columns picker says what is drawn
      // and brings a hidden column back.
      hideBelow: "6xl",
      // Routing agents sort together, under the label their cell shows.
      sortValue: (agent) => agent.skip_llm ? null : agent.llm_model_source === "routing" ? "Routing model" : (agent.llm_model || ""),
      cell: (agent) => <AgentModelLabel agent={agent} />,
    },
    {
      key: "assignments",
      header: "Assignments",
      align: "right",
      // The Resources cell already counts assignments (the Zap figure): the
      // column of its own shows from 1680 px only, so the table, with its
      // "Net value (30d)" column, fits its card at 1536 (1440 px in a 1290 px
      // card with it): shown from an 88rem table (a 1680 px window).
      hideBelow: "8xl",
      className: "whitespace-nowrap tabular-nums text-default-secondary",
      sortValue: (agent) => agent.assignment_count,
      cell: (agent) => formatCompactNumber(agent.assignment_count),
    },
    {
      key: "resources",
      header: "Resources",
      hideBelow: "4xl",
      className: "whitespace-nowrap",
      sortValue: (agent) =>
        Object.values(agent.integration_bindings || {}).flat().length
        + (agent.mcp_server_ids || []).length
        + (agent.knowledge_base_ids || []).length
        + (agent.custom_tool_ids || []).length,
      cell: (agent) => <AgentResourceCounts agent={agent} />,
    },
    // "Net value (30d)" (#9836 R6b, design 10.8): the shared RoiFigure from one batched read; "Value (30d)" when costs are masked.
    ...(roi
      ? [
          {
            key: "roi",
            header: ROI_COLUMN_HEADERS[roiColumnMetric(roi.costMasked)],
            align: "right" as const,
            interactive: true,
            className: "whitespace-nowrap tabular-nums",
            sortValue: (agent: Agent) => summarySortValue(roi.byId.get(agent.id), roi.costMasked),
            cell: (agent: Agent) => (
              <RoiListCell
                item={roi.byId.get(agent.id)}
                currency={roi.currency}
                costMasked={roi.costMasked}
                loading={roi.isLoading}
                declareAgentId={canManageEntity(agent, userId, isAdmin) ? agent.id : undefined}
              />
            ),
          } satisfies EntityTableColumn<Agent>,
        ]
      : []),
    {
      key: "tags",
      header: "Tags",
      // The "Net value (30d)" column (148 px) needs the room: Tags shows from
      // 1440 px, 120 px wide, the rest folded into "+N" (1308 px in a 1194 px
      // card at 1440 with Tags at its natural width; 1174 in 1034 at 1280).
      hideBelow: "6xl",
      className: "w-[120px] max-w-[120px]",
      cell: (agent) => <TagList tags={agent.tags} />,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      interactive: true,
      cell: (agent) => (
        <AgentActions
          agent={agent}
          isAdmin={isAdmin}
          userId={userId}
          chatEnabled={chatEnabled}
          isLinkedGA={linkedGAId === agent.id}
        />
      ),
    },
  ], [t_i18n, isAdmin, userId, activeAgentIds, chatEnabled, linkedGAId, isFavorite, onToggleFavorite, roi]);
}

/**
 * The reader's own override, said once above the list.
 *
 * A per-agent tooltip cannot carry this: nobody hovers twenty agents to find
 * out that none of them will ask them. The pictos keep reporting the agents'
 * own switches — shared state, and what the recap counts — so this is where
 * "…but not for you" belongs.
 */
function GateSuspendedNotice({ agents }: { agents: Agent[] }) {
  const { t_i18n } = useFormatter();
  const { platformOff, allowAll, installDefault } = useGateSuspended();
  /* Only when one of these agents asks: an override of something nothing does
     is noise, and approvals are opt-in. */
  const anyAsks = agents.some((a) => asksBeforeRunningTools(a, installDefault));
  if ((!platformOff && !allowAll) || !anyAsks) return null;
  return (
    <p className="rounded-sm border border-feedback-alert-primary/30 bg-warning-box px-3 py-2 content-caption text-feedback-warning-tertiary">
      {platformOff
        ? t_i18n(
            "An administrator has suspended approvals for the whole platform, so no agent below asks anyone right now.",
          )
        : t_i18n(
            "You allow every tool, so no agent below asks you. They still ask everyone else first.",
          )}
    </p>
  );
}

export default function Agents() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: allLoadedAgents, isLoading } = useAgents();
  // `?fleet=none` (the Fleets list's "Not in a fleet" line, #9977 S2): only the
  // agents with no home fleet; the filter lives in the URL (shareable, back button).
  const [searchParams, setSearchParams] = useSearchParams();
  const notInAFleet = searchParams.get("fleet") === "none";
  const agents = useMemo(
    () => (notInAFleet ? allLoadedAgents?.filter((agent) => !agent.fleet) : allLoadedAgents),
    [allLoadedAgents, notInAFleet],
  );
  const clearFleetFilter = () =>
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("fleet");
      return next;
    });
  const { data: activeAgentIdList } = useActiveAgentIds();
  // Memoized so it stays a stable dependency for the column definitions.
  const activeAgentIds = useMemo(() => new Set(activeAgentIdList ?? []), [activeAgentIdList]);
  const { user, isAdmin } = useAuth();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const linkedGAId = generalAssistantStandIn(gaConfig?.linked_agent_id, agents)?.id ?? null;
  const agenticAIEnabled = useAgenticAIEnabled();
  const chatEnabled = useChatEnabled();

  const [viewMode, setViewMode] = useViewMode("agents");
  const { isFavorite, toggleFavorite } = useFavoriteAgents();
  // One summary read for the whole list, in list view only, never on Community Edition (no ROI there).
  const roiLocked = useEnterpriseLockedReason();
  const agentIds = useMemo(() => (agents ?? []).map((agent) => agent.id), [agents]);
  const roiSummary = useRoiSummary("agent", agentIds, "30d", { enabled: viewMode === "list" && !roiLocked });
  const roi = roiLocked ? null : roiSummary;
  const columns = useAgentColumns({ isAdmin, userId: user?.id, activeAgentIds, chatEnabled, linkedGAId, isFavorite, onToggleFavorite: toggleFavorite, roi });

  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, selectedProperties, setSelectedProperties, allItems: allAgents, allTags, filtered, builtinFold } =
    useEntityFilter({ items: agents, userId: user?.id, properties: AGENT_PROPERTIES, builtin: AGENT_BUILTIN_FOLD });
  // The fold's toggle as one option of the Properties filter, and Company's tooltip while built-ins are hidden.
  const propertyFilter = useBuiltinPropertyFilter(AGENT_PROPERTIES, selectedProperties, setSelectedProperties, builtinFold);
  const companyTip = useBuiltinCompanyTip(builtinFold);

  // Starred agents lead the list and the grid: `EntityTable` pins them per sort, so they
  // stay on top whichever column is sorted. Filters still apply first: a favorite that
  // does not match the search is not shown.
  const pinFavorite = useCallback((agent: Agent) => isFavorite(agent.id), [isFavorite]);

  return (
    <div className="space-y-6">
      {/* One header row (#10215, #9301 N4, as /fleets): the page's help is the title's "i". */}
      <ListPageHeader
        title={t_i18n("Agents")}
        infoLabel={t_i18n("About agents")}
        info={t_i18n("Your AI agents work autonomously with configurable assignments, tools, and knowledge bases.")}
        actions={
          <>
            <AgenticGateDialog feature="Creating agents">
              {/* Default size, as "New Agent" and Presentations' Import: one
                  button height in the title row (design-system doc,
                  "Page-title actions"). */}
              <Button
                variant="ghost"
                className="gap-1.5 text-default-secondary hover:text-default-primary"
                onClick={() => navigate("/agents/new/import")}
              >
                <Upload className="h-4 w-4" /> {t_i18n("Import")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
            <AgenticGateDialog feature="Creating agents">
              <Button className="gap-1.5" onClick={() => navigate("/agents/new")}>
                <Plus className="h-4 w-4" /> {t_i18n("New Agent")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
          </>
        }
      />

        <>
          {allAgents.length > 0 && <GateSuspendedNotice agents={allAgents} />}

          {!isLoading && allAgents.length === 0 ? (
            <EmptyState
              icon={Bot}
              title="No agents yet"
              requiresAi
              description={agenticAIEnabled ? "Create your first agent from a template or from scratch." : "Agents will appear here once they are provisioned."}
              actionLabel={agenticAIEnabled ? "Create Your First Agent" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/agents/new") : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope, Properties,
            // tags, the fleet pill, Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(agent) => agent.id}
              rowHref={(agent) => `/agents/${agent.id}`}
              rowClassName={(agent) => agent.enabled ? undefined : "opacity-60"}
              defaultSortKey="name"
              pinFirst={pinFavorite}
              storageKey="agents"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search agents...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts, tips: companyTip }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              filterPills={
                <>
                  <PropertyFilter options={propertyFilter.options} selected={propertyFilter.selected} onChange={propertyFilter.onChange} />
                  {notInAFleet && (
                // The same chip as the Properties filter (filterPillClass), not a Badge: one component per toolbar (UX r1 B3 on #10010).
                <span className={filterPillClass(true, "pl-2.5 pr-1")} data-testid="agents-fleet-filter">
                  <Boxes className="h-3 w-3" aria-hidden />
                  {t_i18n("Fleet: Not in a fleet")}
                  <button
                    type="button"
                    onClick={clearFleetFilter}
                    aria-label={t_i18n("Clear the fleet filter")}
                    className="rounded-sm p-0.5 text-accent/70 transition-colors hover:bg-accent/15 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  >
                    <X className="h-3 w-3" aria-hidden />
                  </button>
                    </span>
                  )}
                  <GatedAgentsRecap agents={filtered} />
                </>
              }
              view={{ value: viewMode, onChange: setViewMode }}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((agent) => <AgentCard key={agent.id} agent={agent} isAdmin={isAdmin} userId={user?.id} isRunning={activeAgentIds.has(agent.id)} chatEnabled={chatEnabled} isLinkedGA={linkedGAId === agent.id} isFavorite={isFavorite(agent.id)} onToggleFavorite={() => toggleFavorite(agent.id)} />)}
                      </div>
                    )
                  : undefined
              }
              totalAll={allAgents.length}
              externalFilterCount={selectedTags.length + selectedProperties.length + (notInAFleet ? 1 : 0)}
              onClearAll={() => clearAll((next) => { next.delete("fleet"); return next; }, { keepScope: true })}
              noResultsLabel={t_i18n("No agents matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No agents matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}
          {/* Nothing under the records table: it is the page's last block, so it fits the
              window with its own scroller (useFitToViewport). The built-in toggle lives in
              the Properties filter and Company's tooltip counts what it hides (#11905). */}
        </>
    </div>
  );
}
