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
 * The facts a card of the Agentic Flow > Fleets list states beside the fleet
 * itself (#9977 S2, mockups r3 approved under f88d65c6), read from the agents
 * and assignments the page loads once, never one request per card:
 *
 *   - members: the agents whose home fleet is the card's (`Agent.fleet`);
 *   - what starts it: the members' automations in one short line ("Events +
 *     3 schedules"), the enabled ones only when any is on;
 *   - modules: the integration providers the members are bound to (per
 *     integration, per user template, per element) or whose events start one of
 *     their automations, sorted, instance suffix dropped (kept from #9941);
 *   - entry points: the agents outside a fleet that hand over to one of its
 *     exposed agents, i.e. where people enter it;
 *   - not in a fleet: the agents with no home fleet (the line under the grid).
 *
 * Pure: no DOM, no React, no request. The wording is the component's.
 */
import { INTEGRATION_PROVIDER_NAMES } from "./constants";

/**
 * The name a module shows: the integration catalogue's display name, never the
 * internal provider id ("mitre_atlas" reads "MITRE ATLAS"; UX r1 B1 on #10010).
 * An unknown provider falls back to its id with the underscores spaced.
 */
export function moduleDisplayName(provider: string): string {
  return INTEGRATION_PROVIDER_NAMES[provider] ?? provider.replace(/_/g, " ");
}

/** The fields of an assignment a card reads. */
export interface FleetCardAssignment {
  agent_id: string;
  enabled: boolean;
  trigger_type: string;
  trigger_config?: { cron?: string | null; provider?: string | null } | null;
}

/** The fields of an agent a card reads. */
export interface FleetCardAgent {
  id: string;
  name?: string;
  fleet?: { fleet_id: string } | null;
  handover_agent_ids?: readonly string[] | null;
  integration_bindings?: Record<string, readonly string[]> | null;
  integration_template_bindings?: readonly string[] | null;
  integration_element_bindings?: Record<string, readonly unknown[]> | null;
}

/** The fields of a fleet the facts read. */
export interface FleetCardFleet {
  id: string;
  exposed_agent_ids: readonly string[];
}

/** The home members of every fleet, by fleet id. */
export function membersByFleet(agents: readonly FleetCardAgent[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const agent of agents) {
    const fleetId = agent.fleet?.fleet_id;
    if (!fleetId) continue;
    const list = out.get(fleetId);
    if (list) list.push(agent.id);
    else out.set(fleetId, [agent.id]);
  }
  return out;
}

/** What starts a fleet, counted: event-driven automations and schedules. */
export interface FleetStarts {
  /** Some automation starts on an event (an integration or platform event, a webhook, a work item). */
  events: boolean;
  /** Distinct schedules (by cron). */
  schedules: number;
}

/** The `TriggerType`s that start on an event; `manual` starts nothing by itself. */
const EVENT_TRIGGERS = new Set(["integration_event", "platform_event", "webhook", "work_item"]);

/**
 * What starts the fleet: the members' event triggers and distinct schedules.
 * When any automation is on, only the ones on count (what starts it now);
 * otherwise every one (what would start it once set up). Manual triggers
 * start nothing by themselves and are left out.
 */
export function fleetStarts(memberIds: readonly string[], assignments: readonly FleetCardAssignment[]): FleetStarts {
  const members = new Set(memberIds);
  const own = assignments.filter((a) => members.has(a.agent_id));
  const anyOn = own.some((a) => a.enabled);
  const counted = anyOn ? own.filter((a) => a.enabled) : own;
  const crons = new Set<string>();
  let events = false;
  for (const a of counted) {
    if (a.trigger_type === "schedule") crons.add(a.trigger_config?.cron?.trim() || `#${crons.size}`);
    else if (EVENT_TRIGGERS.has(a.trigger_type)) events = true;
  }
  return { events, schedules: crons.size };
}

/** The Platform Health fleet's own facts (`Fleet.health_starts`, #974), as the card reads them. */
interface HealthStartsFacts {
  events: boolean;
  schedules: number;
  modules: readonly string[];
}

/**
 * What starts a card's fleet and what it covers: the Platform Health fleet
 * reads its Settings schedules, signals and covered modules (its assignments
 * are `manual`, started by the Settings cycle runner, #974); every other fleet
 * keeps its members' automations and bindings.
 */
export function cardStartsAndModules(
  healthStarts: HealthStartsFacts | null | undefined,
  starts: FleetStarts,
  modules: readonly string[],
): { starts: FleetStarts; modules: readonly string[] } {
  if (!healthStarts) return { starts, modules };
  return { starts: { events: healthStarts.events, schedules: healthStarts.schedules }, modules: healthStarts.modules };
}

/** The integration providers the members use, sorted by displayed name, instance suffix dropped. */
export function fleetModules(
  memberIds: readonly string[],
  agentsById: ReadonlyMap<string, FleetCardAgent>,
  assignments: readonly FleetCardAssignment[] = [],
): string[] {
  const out = new Set<string>();
  const add = (key: string) => {
    const provider = key.split("#")[0].trim();
    if (provider) out.add(provider);
  };
  for (const id of memberIds) {
    const agent = agentsById.get(id);
    if (!agent) continue;
    for (const [key, ids] of Object.entries(agent.integration_bindings ?? {})) {
      if (ids.length > 0) add(key);
    }
    for (const key of agent.integration_template_bindings ?? []) add(key);
    for (const [key, elements] of Object.entries(agent.integration_element_bindings ?? {})) {
      if (elements.length > 0) add(key);
    }
  }
  const members = new Set(memberIds);
  for (const a of assignments) {
    if (a.trigger_type !== "integration_event" || !members.has(a.agent_id)) continue;
    const provider = a.trigger_config?.provider;
    if (provider) add(provider);
  }
  return [...out].sort((a, b) => moduleDisplayName(a).localeCompare(moduleDisplayName(b)) || a.localeCompare(b));
}

/** An agent people talk to that hands over to fleets: where they enter. */
export interface FleetEntryPoint {
  agentId: string;
  name: string;
  /** The fleets it hands over to, in list order. */
  fleetIds: string[];
}

/**
 * The entry points of the fleets: every agent that hands over to an exposed
 * agent of a fleet it is not a home member of. Sorted by how many fleets each
 * reaches, then by name.
 */
export function fleetEntryPoints(
  fleets: readonly FleetCardFleet[],
  agents: readonly FleetCardAgent[],
): FleetEntryPoint[] {
  const exposedTo = new Map<string, string[]>();
  for (const fleet of fleets) {
    for (const agentId of fleet.exposed_agent_ids) {
      const list = exposedTo.get(agentId);
      if (list) list.push(fleet.id);
      else exposedTo.set(agentId, [fleet.id]);
    }
  }
  const out: FleetEntryPoint[] = [];
  for (const agent of agents) {
    const reached = new Set<string>();
    for (const target of agent.handover_agent_ids ?? []) {
      for (const fleetId of exposedTo.get(target) ?? []) {
        if (fleetId !== agent.fleet?.fleet_id) reached.add(fleetId);
      }
    }
    if (reached.size === 0) continue;
    const order = fleets.map((f) => f.id).filter((id) => reached.has(id));
    out.push({ agentId: agent.id, name: agent.name ?? "", fleetIds: order });
  }
  return out.sort((a, b) => b.fleetIds.length - a.fleetIds.length || a.name.localeCompare(b.name));
}

/** The agents in no fleet: the "Not in a fleet" line under the grid and `/agents?fleet=none`. */
export function agentsNotInAFleet<T extends FleetCardAgent>(agents: readonly T[]): T[] {
  return agents.filter((agent) => !agent.fleet);
}

/** The sorts of the Fleets list, kept in the URL (`?sort=`); the default is the name. */
export type FleetSort = "name" | "status" | "last_run" | "value";

const STATUS_RANK: Record<string, number> = { on: 0, partial: 1, paused: 2, not_set_up: 3 };

/** Orders fleets by `sort`: name A to Z, status On first, last run newest first (never run last). */
export function sortFleets<T extends { name: string; last_run_at?: string | null }>(
  fleets: readonly T[],
  sort: FleetSort,
  statusOf: (fleet: T) => string | null,
  /** The fleet's net value over 30 days (gross when costs are masked), `null` without a figure (#9836 R6c). */
  valueOf: (fleet: T) => number | null = () => null,
): T[] {
  const byName = (a: T, b: T) => a.name.localeCompare(b.name);
  const copy = [...fleets];
  if (sort === "status") {
    return copy.sort(
      (a, b) => (STATUS_RANK[statusOf(a) ?? ""] ?? 4) - (STATUS_RANK[statusOf(b) ?? ""] ?? 4) || byName(a, b),
    );
  }
  if (sort === "value") {
    // Highest first; a fleet without a figure after every figure, then by name.
    const figure = (f: T) => valueOf(f) ?? Number.NEGATIVE_INFINITY;
    return copy.sort((a, b) => (figure(b) === figure(a) ? 0 : figure(b) > figure(a) ? 1 : -1) || byName(a, b));
  }
  if (sort === "last_run") {
    const at = (f: T) => (f.last_run_at ? Date.parse(f.last_run_at) : Number.NEGATIVE_INFINITY);
    return copy.sort((a, b) => at(b) - at(a) || byName(a, b));
  }
  return copy.sort(byName);
}

/** Reads `?sort=`: an unknown value falls back to the name. */
export function readFleetSort(value: string | null): FleetSort {
  return value === "status" || value === "last_run" || value === "value" ? value : "name";
}
