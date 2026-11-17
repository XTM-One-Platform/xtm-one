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
 * "Fleet" on an agent's Overview, the agent's About card (#9539 S5): the
 * agent's home fleet as a small hero (fleet name, the fleet's purpose line
 * from its documentation, who the agent reports to, its role label,
 * orchestrator and exposure), the agent's own automations (on of total) and
 * a link to the fleet's About page, then the fleets that connect it. Read
 * only: every edit lives on the fleet page, which the card links to. Reading
 * it starts no run and calls no model. Not drawn when the fleets read is
 * refused for the licence (403): the Overview stays as it was; any other
 * failed read is said on the card.
 */
import { Link } from "react-router-dom";
import { ArrowRight, BookOpen, Network, Plus, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAgentFleets, useFleetDocumentation } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import {
  agentAutomations,
  contextLabel,
  fleetAboutPath,
  fleetPath,
  hidesFleetCard,
  homeChips,
  isHomeHidden,
  isInPlayground,
  offersAddAutomation,
  reportsTo,
} from "./agent-fleet-card";
import type { Agent, Assignment } from "@/types";

export function AgentFleetCard({
  agent,
  assignments,
  onAddAssignment,
}: {
  agent: Agent;
  assignments?: Assignment[];
  /** Opens this page's Add Assignment panel; absent when the reader cannot add one. */
  onAddAssignment?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { data, error, isError, isLoading } = useAgentFleets(agent.id);
  // The home fleet's documentation, for its purpose line: a read only, no run, no model call.
  const { data: doc } = useFleetDocumentation(data?.home?.fleet_id);

  if (isLoading || (isError && hidesFleetCard(error))) return null;

  const home = data?.home ?? null;
  const links = data?.connected_to ?? [];
  const parent = home ? reportsTo(home) : null;
  const purpose = doc?.structured?.purpose_line?.trim() ?? "";
  const automations = agentAutomations(assignments);

  return (
    <Card data-testid="agent-fleet-card">
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Network className="h-4 w-4 text-accent" /> {t_i18n("Fleet")}
        </CardTitle>
        {home && (
          <Link
            to={fleetPath(home.fleet_id)}
            className="content-compact flex items-center gap-1 text-accent hover:underline"
          >
            {home.can_manage ? t_i18n("Edit in the fleet") : t_i18n("Open the fleet")} <ArrowRight className="h-3 w-3" />
          </Link>
        )}
      </CardHeader>
      <CardContent className="space-y-3 px-4 pb-4 pt-0">
        {isError ? (
          <p className="content-compact text-default-secondary" role="status">
            {t_i18n("The fleet could not be loaded.")}
          </p>
        ) : isInPlayground(data) ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("In no fleet: this agent is in the playground.")}{" "}
            <Link to="/fleets" className="text-accent hover:underline">
              {t_i18n("Open Fleets")}
            </Link>
          </p>
        ) : null}
        {isHomeHidden(data) && (
          <p className="content-compact text-default-secondary">{t_i18n("In a fleet you cannot see.")}</p>
        )}
        {home && parent && (
          <div className="space-y-3" data-testid="agent-fleet-about">
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <Link
                  to={fleetPath(home.fleet_id)}
                  className="content-base font-medium text-default-primary hover:underline"
                >
                  {home.fleet_name}
                </Link>
                {homeChips(home).map((chip) => (
                  <Badge key={chip} variant="outline">
                    {t_i18n(chip)}
                  </Badge>
                ))}
              </div>
              {purpose && (
                <p className="content-compact text-default-primary" data-testid="agent-fleet-purpose">
                  {purpose}
                </p>
              )}
              <p className="content-compact text-default-secondary">
                {parent.key === "Reports to {name}" ? t_i18n(parent.key, { name: parent.name }) : t_i18n(parent.key)}
                {home.role_label ? ` · ${home.role_label}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              {automations && (
                <div className="flex flex-wrap items-center gap-2">
                  <p className="content-compact flex items-center gap-1.5 text-default-secondary" data-testid="agent-fleet-automations">
                    <Zap className="h-4 w-4 text-accent" aria-hidden />
                    {automations.total > 0
                      ? t_i18n("Its automations: {on} of {total} on", { on: automations.on, total: automations.total })
                      : t_i18n("No automation yet")}
                  </p>
                  {onAddAssignment && offersAddAutomation(automations, Boolean(onAddAssignment)) && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 content-compact gap-1.5"
                      onClick={onAddAssignment}
                      data-testid="agent-fleet-add-automation"
                    >
                      <Plus className="h-3 w-3" aria-hidden /> {t_i18n("Add Assignment")}
                    </Button>
                  )}
                </div>
              )}
              <Link
                to={fleetAboutPath(home.fleet_id)}
                className="content-compact flex items-center gap-1.5 text-accent hover:underline"
                data-testid="agent-fleet-about-link"
              >
                <BookOpen className="h-4 w-4" aria-hidden />
                {t_i18n("About the fleet")}
              </Link>
            </div>
          </div>
        )}
        {links.length > 0 && (
          <div className="space-y-1.5">
            <p className="content-caption text-default-secondary">{t_i18n("Connected to")}</p>
            <ul className="space-y-1">
              {links.map((link) => (
                <li key={link.member_id} className="flex flex-wrap items-center gap-2">
                  <Link to={fleetPath(link.fleet_id)} className="content-compact text-default-primary hover:underline">
                    {link.fleet_name}
                  </Link>
                  <Badge variant="secondary">{t_i18n(contextLabel(link))}</Badge>
                  {link.is_archived && <Badge variant="outline">{t_i18n("Archived")}</Badge>}
                  {link.collaboration_instruction && (
                    <span className="content-caption min-w-0 truncate text-default-secondary" title={link.collaboration_instruction}>
                      {link.collaboration_instruction}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
