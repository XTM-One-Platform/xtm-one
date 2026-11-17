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

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Bot, PauseCircle, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { usePlatformSettings, useUpdatePlatformSettings } from "@/hooks/api/useAiAdoptionApi";
import { SECURITY_AUDIT_QUERY_KEY } from "@/hooks/api/useSecurityAuditApi";
import {
  TOOL_APPROVAL_USAGE_QUERY_KEY,
  useToolApprovalUsage,
  type ToolApprovalUsage,
} from "@/hooks/api/useToolApprovals";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { ToolApprovalList } from "@/pages/tools/ToolApprovalList";
import { PlatformApprovalSection } from "./PlatformApprovalSection";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Settings > Access and security > Tool approvals: the ONE home of the platform's
// side of tool approvals - whether they are in use and who asks, the default
// every agent follows, the per-tool rules, the connected platforms' policies
// and the emergency suspend switch. A person's own approvals are on their
// profile. The Security audit reports the switches as the
// agents.tool_approvals and agents.tool_approvals_default controls.
// ---------------------------------------------------------------------------

/** How many agents may be listed by name before the rest is a count. */
const LISTED_AGENTS = 12;

export function ToolApprovalsSection() {
  const { t_i18n } = useFormatter();
  const { data: settings, isLoading } = usePlatformSettings();
  const { data: usage } = useToolApprovalUsage();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const rulesRef = useRef<HTMLDivElement>(null);
  /* A `?tool=` link (a tool's page, an integration's tool table) is about one
     rule, so it lands on the rules rather than on the switches above them. */
  const landsOnRule = useRef(params.has("tool"));
  useEffect(() => {
    if (!isLoading && landsOnRule.current) {
      landsOnRule.current = false;
      rulesRef.current?.scrollIntoView({ block: "start" });
    }
  }, [isLoading]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  const enabled = settings?.tool_approvals_enabled !== false;
  const onSaved = () => {
    qc.invalidateQueries({ queryKey: ["platform-settings"] });
    qc.invalidateQueries({ queryKey: ["tool-approvals"] });
    qc.invalidateQueries({ queryKey: TOOL_APPROVAL_USAGE_QUERY_KEY });
    qc.invalidateQueries({ queryKey: ["tool-gate-catalog"] });
    qc.invalidateQueries({ queryKey: SECURITY_AUDIT_QUERY_KEY });
  };

  return (
    <div className="space-y-6">
      <ApprovalsOverviewCard
        enabled={enabled}
        agentsDefault={settings?.approve_tool_calls_default === true}
        usage={usage}
        onSaved={onSaved}
      />
      <div ref={rulesRef} className="scroll-mt-4">
        <Card>
          <CardHeader className="pb-3">
            {/* The words, not the mechanism: "whitelist" and "gate" named the
                implementation and left the reader to translate. */}
            <CardTitle className="font-sans-geologica flex items-center gap-2">
              <HitlIcon state="gated" className="h-4 w-4" decorative />
              {t_i18n("Tool rules")}
            </CardTitle>
            <CardDescription className="content-compact">
              {t_i18n(
                "Read-only tools run without asking; every other tool asks first. Change that for one tool here: the rule applies to every agent and every person on this platform.",
              )}
            </CardDescription>
            {usage && !usage.configured && (
              <p className="content-compact text-default-secondary">
                {t_i18n(
                  "These rules change nothing yet: they take effect once an agent asks before running tools.",
                )}
              </p>
            )}
          </CardHeader>
          <CardContent className="pt-0">
            <ToolApprovalList scope="platform" />
          </CardContent>
        </Card>
      </div>
      {/* Per-platform policies only exist to tune approvals that can run -
          while approvals are suspended they would be dead controls. */}
      {enabled && <PlatformApprovalSection />}
      <SuspendCard enabled={enabled} usage={usage} onSaved={onSaved} />
    </div>
  );
}

/**
 * Whether approvals are in use, who asks, and the default every agent follows.
 *
 * The status is computed by the backend (`GET /tool-approvals/usage`) with the
 * gate's own rule, so "not in use" here means no tool call can wait for a
 * person on this platform, and the list of agents is the exact set that asks.
 */
function ApprovalsOverviewCard({
  enabled,
  agentsDefault,
  usage,
  onSaved,
}: {
  enabled: boolean;
  /** What an agent with no approval setting of its own does. */
  agentsDefault: boolean;
  usage: ToolApprovalUsage | undefined;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdatePlatformSettings();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const following = usage?.agents_following_default ?? 0;
  const asking = usage?.asking_agents ?? [];
  const assistantAsks = usage?.general_assistant_asks ?? false;
  /* The default reaches the General Assistant only where chat is available;
     until the usage loads, the wording keeps naming it. */
  const assistantFollows = usage?.general_assistant_available ?? true;
  const state: "suspended" | "in_use" | "not_in_use" | "loading" = !usage
    ? "loading"
    : !enabled
    ? "suspended"
    : usage.in_use
    ? "in_use"
    : "not_in_use";

  const handleDefaultChange = async (next: boolean) => {
    if (next === agentsDefault) return;
    const ok = await confirm(
      next
        ? {
            title: t_i18n("Make every agent ask before running tools?"),
            description: assistantFollows
              ? t_i18n(
                  "{n, plural, =0 {The General Assistant} one {# agent left on Follow the platform default, and the General Assistant,} other {# agents left on Follow the platform default, and the General Assistant,}} will wait for a person before a tool call that is not known to be read-only. Scheduled runs wait until someone approves. The change is recorded in the audit log.",
                  { n: following },
                )
              : t_i18n(
                  "{n, plural, =0 {No agent is left on Follow the platform default today; any agent left on it later will wait for a person before a tool call that is not known to be read-only.} one {# agent left on Follow the platform default will wait for a person before a tool call that is not known to be read-only.} other {# agents left on Follow the platform default will wait for a person before a tool call that is not known to be read-only.}} Scheduled runs wait until someone approves. The change is recorded in the audit log.",
                  { n: following },
                ),
            confirmLabel: t_i18n("Turn on"),
            variant: "warning",
          }
        : {
            title: t_i18n("Stop asking by default?"),
            description: assistantFollows
              ? t_i18n(
                  "Every agent left on Follow the platform default, and the General Assistant, will run its tools without asking anyone. Agents set to Ask keep asking. The change is recorded in the audit log.",
                )
              : t_i18n(
                  "Every agent left on Follow the platform default will run its tools without asking anyone. Agents set to Ask keep asking. The change is recorded in the audit log.",
                ),
            confirmLabel: t_i18n("Stop asking by default"),
            variant: "warning",
          },
    );
    if (!ok) return;
    update.mutate(
      { approve_tool_calls_default: next },
      {
        onSuccess: () => {
          toast({
            title: next
              ? t_i18n("Every agent now asks before running tools by default")
              : t_i18n("Agents no longer ask by default"),
            variant: "success",
          });
          onSaved();
        },
        onError: () => {
          toast({
            title: t_i18n("Could not change the approval setting"),
            variant: "error",
          });
        },
      },
    );
  };

  return (
    <Card>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="font-sans-geologica flex items-center gap-2">
          <HitlIcon state="topic" className="h-4 w-4" decorative />
          {t_i18n("Tool approvals")}
          {state !== "loading" && (
            <Badge
              variant={state === "in_use" ? "success" : state === "suspended" ? "warning" : "outline"}
              className="ml-1 content-caption"
              data-testid="tool-approvals-status"
              data-state={state}
            >
              {state === "in_use"
                ? t_i18n("In use")
                : state === "suspended"
                ? t_i18n("Suspended")
                : t_i18n("Not in use")}
            </Badge>
          )}
        </CardTitle>
        <CardDescription className="content-compact leading-relaxed">
          {t_i18n(
            "With tool approvals, an agent stops before a tool call that is not known to be read-only and waits until a person approves it: in chat, in channels and on scheduled runs. They are opt-in: nothing asks until you turn them on for an agent, or for every agent below.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {state === "not_in_use" && (
          <p className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2.5 content-compact text-default-secondary">
            {t_i18n(
              "No agent asks before running tools, so every tool call runs as it always has.",
            )}
          </p>
        )}
        {state === "suspended" && (
          <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2.5 content-compact text-feedback-warning-tertiary">
            {usage?.configured
              ? t_i18n(
                  "Suspended: every tool call runs without asking, for everyone. The agents set to ask will ask again once you resume, at the bottom of this page.",
                )
              : t_i18n("Suspended: every tool call runs without asking, for everyone.")}
          </p>
        )}
        {(state === "in_use" || (state === "suspended" && usage?.configured)) && (
          <div className="space-y-2">
            <p className="content-compact font-medium">
              {state === "in_use"
                ? assistantAsks
                  ? t_i18n(
                      "{n, plural, =0 {The General Assistant asks before running tools:} one {The General Assistant and # agent ask before running tools:} other {The General Assistant and # agents ask before running tools:}}",
                      { n: asking.length },
                    )
                  : t_i18n(
                      "{n, plural, =0 {Asks before running tools:} one {# agent asks before running tools:} other {# agents ask before running tools:}}",
                      { n: asking.length },
                    )
                : assistantAsks
                ? t_i18n(
                    "{n, plural, =0 {The General Assistant is set to ask:} one {The General Assistant and # agent are set to ask:} other {The General Assistant and # agents are set to ask:}}",
                    { n: asking.length },
                  )
                : t_i18n(
                    "{n, plural, =0 {Set to ask:} one {# agent is set to ask:} other {# agents are set to ask:}}",
                    { n: asking.length },
                  )}
            </p>
            <div className="flex flex-wrap gap-1.5" data-testid="tool-approvals-asking">
              {assistantAsks && (
                <span className="inline-flex items-center gap-1 rounded-sm border border-accent/30 bg-accent/10 px-2 py-0.5 content-compact text-accent">
                  <Sparkles className="size-3" />
                  {t_i18n("General Assistant")}
                </span>
              )}
              {asking.slice(0, LISTED_AGENTS).map((agent) => (
                <Link
                  key={agent.id}
                  to={`/agents/${agent.id}/config/tooling`}
                  className="inline-flex items-center gap-1 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-0.5 content-compact hover:bg-elevation-highlight"
                >
                  <Bot className="size-3 text-default-secondary" />
                  {agent.name}
                  {agent.follows_default && (
                    <span className="text-default-secondary">· {t_i18n("by default")}</span>
                  )}
                </Link>
              ))}
              {asking.length > LISTED_AGENTS && (
                <span className="px-1 py-0.5 content-compact text-default-secondary">
                  {t_i18n("and {n} more", { n: asking.length - LISTED_AGENTS })}
                </span>
              )}
            </div>
          </div>
        )}

        <div
          className={cn(
            "flex items-center justify-between gap-4 rounded-sm border px-3 py-2.5",
            agentsDefault ? "border-feedback-success-primary/25 bg-feedback-success-primary/[0.04]" : "border-elevation-subtle-soft bg-elevation-hover",
          )}
        >
          <div className="min-w-0">
            <p className="content-base font-medium">{t_i18n("Every agent asks by default")}</p>
            <p className="content-compact text-default-secondary">
              {assistantFollows
                ? t_i18n(
                    "Applies to the General Assistant and to every agent left on Follow the platform default ({n, plural, one {# agent} other {# agents}} today). An agent set to Ask or Don't ask on its Configuration tab keeps its choice.",
                    { n: following },
                  )
                : t_i18n(
                    "Applies to every agent left on Follow the platform default ({n, plural, one {# agent} other {# agents}} today). An agent set to Ask or Don't ask on its Configuration tab keeps its choice.",
                    { n: following },
                  )}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {update.isPending && <Spinner size="sm" tone="inherit" className="text-accent" />}
            <Switch
              checked={agentsDefault}
              onCheckedChange={handleDefaultChange}
              disabled={update.isPending}
              aria-label={t_i18n("Every agent asks by default")}
            />
          </div>
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n(
            "To have only some agents ask, leave this off and set Ask before running tools on each of them (the agent > Configuration > Tooling). People can always allow a tool for themselves from their profile.",
          )}
        </p>
      </CardContent>
    </Card>
  );
}

/**
 * The platform-wide emergency switch: suspend every approval at once.
 *
 * Last on the page on purpose — it is not how approvals are turned on or off
 * day to day (that is the default above and each agent's own setting), it is
 * how they are all lifted at once. Suspending is the loud direction and goes
 * through a confirm. Rules, policies and personal approvals are kept, so
 * resuming restores the exact previous posture.
 */
function SuspendCard({
  enabled,
  usage,
  onSaved,
}: {
  enabled: boolean;
  usage: ToolApprovalUsage | undefined;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdatePlatformSettings();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const apply = async (suspend: boolean) => {
    if (suspend === !enabled) return;
    if (suspend) {
      const ok = await confirm({
        title: t_i18n("Suspend tool approvals for the whole platform?"),
        description: t_i18n(
          "No tool call will wait for anyone: every user, every agent, every scheduled run, tools that delete data or act outside the platform included. Rules and personal approvals are kept and apply again when you resume.",
        ),
        confirmLabel: t_i18n("Suspend approvals"),
        variant: "danger",
      });
      if (!ok) return;
    }
    update.mutate(
      { tool_approvals_enabled: !suspend },
      {
        onSuccess: () => {
          toast({
            title: suspend
              ? t_i18n("Tool approvals suspended for the whole platform")
              : t_i18n("Tool approvals resumed"),
            variant: "success",
          });
          onSaved();
        },
        onError: () => {
          toast({
            title: t_i18n("Could not change the approval setting"),
            variant: "error",
          });
        },
      },
    );
  };

  return (
    <Card className={cn(!enabled && "border-feedback-alert-primary/30")}>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="font-sans-geologica flex items-center gap-2 content-base">
          <PauseCircle className={cn("h-4 w-4", enabled ? "text-default-secondary" : "text-feedback-warning-primary")} />
          {t_i18n("Suspend all approvals")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="flex items-center justify-between gap-4">
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "An emergency switch: while suspended, every tool call runs without asking, for everyone, agents set to ask included. Every rule and every personal approval is kept for when you resume. Calls still record that they ran because approvals were suspended.",
            )}
          </p>
          <div className="flex shrink-0 items-center gap-2">
            {update.isPending && <Spinner size="sm" tone="inherit" className="text-accent" />}
            {enabled ? (
              <Switch
                checked={false}
                onCheckedChange={(on) => apply(on)}
                disabled={update.isPending}
                aria-label={t_i18n("Suspend all approvals")}
              />
            ) : (
              <Button
                size="sm"
                variant="outline"
                className="h-8 content-compact"
                disabled={update.isPending}
                onClick={() => apply(false)}
              >
                {t_i18n("Resume approvals")}
              </Button>
            )}
          </div>
        </div>
        {!enabled && usage?.configured && (
          <p className="content-caption text-feedback-warning-primary">
            {usage.general_assistant_asks
              ? t_i18n(
                  "{n, plural, =0 {The General Assistant is set to ask and will ask again once you resume.} one {The General Assistant and # agent are set to ask and will ask again once you resume.} other {The General Assistant and # agents are set to ask and will ask again once you resume.}}",
                  { n: usage.asking_agents.length },
                )
              : t_i18n(
                  "{n, plural, one {# agent is set to ask and will ask again once you resume.} other {# agents are set to ask and will ask again once you resume.}}",
                  { n: usage.asking_agents.length },
                )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
