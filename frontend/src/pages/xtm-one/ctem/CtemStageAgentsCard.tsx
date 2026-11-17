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

/**
 * Vertical-aware CTEM analyst overrides (XTM One, EE-gated).
 *
 * Each CTEM stage is assessed by a seeded analyst (ctem-{stage}-analyst) and the
 * dashboard briefing by the seeded exposure-briefing-analyst. With the agentic
 * AI capability, an admin can set a platform-wide default analyst per stage /
 * briefing PER VERTICAL (so a FIMI deployment and a Cyber one can each use their
 * own agent), and every user can override those defaults with their own agent.
 *
 * Two scopes share this card:
 *   - "platform": admin defaults  -> PlatformSettings (/ctem-metrics/stage-agents)
 *   - "user":     personal override -> User           (/ctem-metrics/my-stage-agents)
 * Resolution at runtime is user override -> platform default -> seeded analyst.
 */
import { useMemo, useState } from "react";
import { ChevronDown, Workflow, GitFork, Newspaper } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import {
  AgenticGateDialog,
  AgenticEEBadgeForButton,
} from "@/components/ui/license-gate-dialog";
import {
  useStageAgents,
  useSetStageAgents,
  type CtemAgentScope,
} from "@/hooks/api/useCtemMetricsApi";
import { useAgents, useDuplicateAgent } from "@/hooks/api/useAgentApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { CtemStageAgentRow, CtemVertical } from "@/types";
import { VerticalSwitcher } from "./VerticalSwitcher";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

const DEFAULT_VALUE = "__default__";

export function CtemStageAgentsCard({
  scope = "platform",
}: {
  scope?: CtemAgentScope;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const isUser = scope === "user";
  const [vertical, setVertical] = useState<CtemVertical>("cyber");
  const { data, isLoading } = useStageAgents(scope, vertical);
  const { data: agents } = useAgents();
  const setOverrides = useSetStageAgents(scope);
  const duplicate = useDuplicateAgent();
  const { toast } = useToast();
  // Assigning / forking a custom analyst is an agentic-AI (EE) capability, gated
  // exactly like "New agent" / "New KB": the EE badge + license popup, no toast.
  const agenticEnabled = useAgenticAIEnabled();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  // Briefing analyst first, then the five lifecycle stages.
  const rows = useMemo<CtemStageAgentRow[]>(
    () => (data ? [data.briefing, ...data.stages] : []),
    [data],
  );
  const agentOptions = agents ?? [];
  const defaultLabel = isUser ? t_i18n("Default (platform)") : t_i18n("Default (seeded)");

  const assign = (key: string, agentId: string | null) =>
    setOverrides.mutate(
      { vertical, overrides: { [key]: agentId } },
      {
        onError: (err) =>
          toast({
            title: t_i18n("Could not update analyst"),
            description: err instanceof Error ? err.message : undefined,
            variant: "error",
          }),
      },
    );

  // Fork the inherited default into a fresh agent (platform scope: the seeded
  // analyst; user scope: the effective platform/seeded agent), then assign it.
  const fork = async (key: string, defaultAgentId: string | null) => {
    if (!defaultAgentId) return;
    setBusyKey(key);
    try {
      const dup = await duplicate.mutateAsync(defaultAgentId);
      await setOverrides.mutateAsync({ vertical, overrides: { [key]: dup.id } });
      if (dup.warnings?.length) {
        toast({
          title: t_i18n("Agent duplicated"),
          description: [
            t_i18n("Some bindings of the original agent were not copied:"),
            ...dup.warnings,
          ].join(" "),
          variant: "info",
        });
      }
    } catch (err) {
      toast({
        title: t_i18n("Could not fork analyst"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <Card>
      <CardContent className="p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Workflow className="h-4 w-4 text-accent" />
            <h3 className="content-base font-semibold">
              {isUser ? t_i18n("My CTEM analysts") : t_i18n("CTEM analysts (platform defaults)")}
            </h3>
          </div>
          <VerticalSwitcher value={vertical} onChange={setVertical} size="sm" />
        </div>
        <p className="content-compact text-default-secondary">
          {isUser
            ? t_i18n("Use your own agent for the Exposure Briefing Analyst or any CTEM stage in this vertical, overriding the platform default. Fork the default into a custom agent and assign it. Requires at least one Enterprise product.")
            : t_i18n("Set the default analyst for the Exposure Briefing Analyst and each CTEM stage in this vertical, for all users. Fork a seeded analyst into a custom agent and assign it; users can still override with their own. Requires at least one Enterprise product.")}
        </p>

        {isLoading ? (
          <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>
        ) : (
          <div className="divide-y divide-[var(--border-elevation-subtle)]">
            {rows.map((row) => {
              const isBriefing = row.stage === "briefing";
              const overridden = !!row.override_agent_id;
              return (
                <div key={row.stage} className="flex items-center gap-3 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="content-base font-medium flex items-center gap-1.5">
                      {isBriefing && (
                        <Newspaper className="h-3.5 w-3.5 text-accent/70 shrink-0" />
                      )}
                      {row.label}
                    </p>
                    <p className="text-[11px] text-default-secondary truncate">
                      {row.effective_agent_name ?? "-"}{" "}
                      <span className="text-default-secondary">
                        ({overridden ? t_i18n("custom") : isUser ? t_i18n("platform default") : t_i18n("default")})
                      </span>
                    </p>
                  </div>
                  {/* Assigning a custom analyst is an Agentic AI capability. When
                      it's off, the field is a button that surfaces the license
                      popup (like Fork): a typeable field would open its list from
                      the keyboard, which a click gate never sees. */}
                  <AgenticGateDialog feature="Customizing CTEM analysts">
                    {agenticEnabled ? (
                      <SearchableSelect
                        aria-label={t_i18n("Analyst for {stage}", { stage: row.label })}
                        className="w-60 shrink-0"
                        options={agentOptions.map((a) => agentSelectOption(a))}
                        pinned={[{ value: DEFAULT_VALUE, label: defaultLabel }]}
                        value={row.override_agent_id ?? DEFAULT_VALUE}
                        onValueChange={(v) => assign(row.stage, v === DEFAULT_VALUE ? null : v)}
                        disabled={aiOff}
                        placeholder={t_i18n("Search agents...")}
                      />
                    ) : (
                      <Button
                        variant="outline"
                        className="w-60 shrink-0 justify-between"
                        aria-label={t_i18n("Analyst for {stage}", { stage: row.label })}
                      >
                        <span className="truncate">
                          {row.override_agent_id
                            ? (agentOptions.find((a) => a.id === row.override_agent_id)?.name ?? row.effective_agent_name ?? defaultLabel)
                            : defaultLabel}
                        </span>
                        <ChevronDown className="h-4 w-4 shrink-0 opacity-60" />
                      </Button>
                    )}
                  </AgenticGateDialog>
                  <AgenticGateDialog feature="Customizing CTEM analysts">
                    <Button
                      variant="outline"
                      size="sm"
                      className="gap-1.5 shrink-0"
                      onClick={() => fork(row.stage, row.default_agent_id)}
                      disabled={busyKey === row.stage || !row.default_agent_id || aiOff}
                    >
                      <GitFork className="h-3.5 w-3.5" /> {t_i18n("Fork")}
                      <AgenticEEBadgeForButton />
                    </Button>
                  </AgenticGateDialog>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
