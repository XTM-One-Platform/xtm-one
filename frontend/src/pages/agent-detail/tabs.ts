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
 * The agent page tabs, in order, for what the viewer may see. A plain module
 * so the list is pinned by `tabs.test.tsx`: a surface that wants a new tab here
 * has to say so there. What an agent will do next is the Overview's
 * "Coming up" card, and its scheduled runs are in Activity: no tab of their own.
 * What the agent returns (its ROI) and what one run may spend (its budget) share
 * the Value tab (#11037); the former Budget tab and Overview ROI links land there.
 */
import {
  Activity,
  ArrowRightLeft,
  BarChart3,
  Brain,
  FlaskConical,
  History,
  MessagesSquare,
  Scan,
  Settings2,
  ThumbsUp,
  Wallet,
} from "lucide-react";

export type AgentTab =
  | "overview"
  | "config"
  | "value"
  | "versioning"
  | "memory"
  | "chats"
  | "delegations"
  | "evaluation"
  | "observability"
  | "feedback"
  | "activity";

const AGENT_TAB_KEYS: readonly AgentTab[] = [
  "overview",
  "config",
  "value",
  "versioning",
  "memory",
  "chats",
  "delegations",
  "evaluation",
  "observability",
  "feedback",
  "activity",
];

/**
 * Whether a `:tab` path segment names an agent tab (none is Overview). An
 * unknown one, the former `budget` included, is not found: no redirect (#797).
 * A known tab the viewer may not see keeps the page, so a flag still resolving
 * never flashes a not-found.
 */
export function isAgentTab(segment: string | undefined): boolean {
  return segment === undefined || (AGENT_TAB_KEYS as readonly string[]).includes(segment);
}

interface AgentTabAccess {
  canManageMemory: boolean;
  canViewChats: boolean;
  canViewFeedback: boolean;
  /** The `EVALUATION` flag, served only where evaluation may run. */
  evaluation: boolean;
  isAdmin: boolean;
}

export function agentDetailTabs({
  canManageMemory,
  canViewChats,
  canViewFeedback,
  evaluation,
  isAdmin,
}: AgentTabAccess): { key: AgentTab; label: string; icon: React.ElementType }[] {
  return [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    // Right after Configuration: the agent's ROI first, then its run budget.
    // A budget IS configuration, but it is the one part of it a non-admin may
    // not freely widen, so it sits beside what the agent returns (#11037).
    { key: "value", label: "Value", icon: Wallet },
    { key: "versioning", label: "Versioning", icon: History },
    ...(canManageMemory ? [{ key: "memory" as AgentTab, label: "Memory", icon: Brain }] : []),
    // Its traces hold the agent's conversations, so they follow the chats rule
    // the server serves.
    ...(canViewChats ? [{ key: "observability" as AgentTab, label: "Observability", icon: Scan }] : []),
    // Every rating of the agent's work: its managers.
    ...(canViewFeedback ? [{ key: "feedback" as AgentTab, label: "Feedback", icon: ThumbsUp }] : []),
    ...(canViewChats ? [{ key: "chats" as AgentTab, label: "Chats", icon: MessagesSquare }] : []),
    { key: "delegations", label: "Delegations", icon: ArrowRightLeft },
    ...(evaluation && isAdmin ? [{ key: "evaluation" as AgentTab, label: "Evaluation", icon: FlaskConical }] : []),
    { key: "activity", label: "Activity", icon: Activity },
  ];
}
