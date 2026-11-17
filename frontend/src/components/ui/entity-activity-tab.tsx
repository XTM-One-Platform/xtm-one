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
 * Shared Activity tab for entity detail pages (integrations, MCP servers, custom tools,
 * skills, prompts): the runs of the agents bound to the entity, on the shared runs
 * table (`RunActivityList`, #9378 slice 4a: Status visible on the toolbar, columns,
 * sort, one pager, state in the URL).
 *
 * **A2A integrations are filtered differently, and have to be.** Their traffic is
 * work a partner submitted, and two A2A integrations bound to the same agent
 * share every agent-scoped query — so bound-agent filtering shows each of them
 * the other's activity, plus every internal run of those agents. Passing
 * `a2aIntegrationId` scopes server-side to the calling registration stamped on
 * each run instead, which is the same attribution the integration's
 * last-activity figure uses.
 */
import { RunActivityList } from "@/components/ui/run-activity-list";
import { exceedsRunAgentFilter } from "@/lib/run-agent-filter";
import { useFormatter } from "@/i18n";

interface EntityActivityTabProps {
  /** Set of agent IDs that are bound to this entity. Ignored when `a2aIntegrationId` is set. */
  boundAgentIds: Set<string>;
  /** Label for the empty state (e.g. "MCP server", "custom tool", "integration"). */
  entityLabel: string;
  /**
   * An A2A integration id. Switches this tab to showing only the work THAT
   * partner submitted, scoped server-side by calling registration rather than
   * by bound agents.
   */
  a2aIntegrationId?: string;
}

export function EntityActivityTab({ boundAgentIds, entityLabel, a2aIntegrationId }: EntityActivityTabProps) {
  const { t_i18n } = useFormatter();

  // A partner registration scopes the runs on its own: filtering by bound agents
  // there would be wrong as well as redundant, since an agent unbound after the
  // fact still has that partner's history to show.
  if (!a2aIntegrationId && exceedsRunAgentFilter(boundAgentIds)) {
    return (
      <div className="py-8 text-center">
        <p className="content-base text-default-secondary">
          {t_i18n("Too many agents use this to list their runs together. Open an agent's Activity tab to see its runs.")}
        </p>
      </div>
    );
  }

  return (
    <RunActivityList
      storageKey={a2aIntegrationId ? "a2a-integration" : "entity"}
      entityLabel={entityLabel}
      agentIds={a2aIntegrationId ? undefined : boundAgentIds}
      a2aIntegrationId={a2aIntegrationId}
      emptyMessage={
        a2aIntegrationId
          ? t_i18n("No activity yet. Work this partner submits over A2A will appear here: only its own, never another partner's.")
          : undefined
      }
    />
  );
}
