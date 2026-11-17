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
 * Who does a job one built-in tool provides: the pure half of
 * `useToolAgents`, tested by `tool-agents.test.ts`.
 *
 * A job is a suite id (`video_studio`) or, for a job that is one sub-area of a
 * merged group, the function it needs (`{ suite: "platform", fn: "create_fleet" }`): an agent carries it through the group or one of its [DEPRECATED]
 * former ids (`aliases`), and its allow list for the group must keep that
 * function.
 */
import { canonicalSuiteId } from "./builtin-tool-aliases.ts";
import type { Agent } from "../types/index.ts";

/** A job: the suite to carry, and the function of it the job needs, if one. */
export interface ToolJob {
  suite: string;
  fn?: string;
}

/** Former id -> group id (`aliasIndex`); empty when the registry is not read yet. */
type AliasMap = ReadonlyMap<string, string>;

const NO_ALIASES: AliasMap = new Map();

function asJob(job: string | ToolJob): ToolJob {
  return typeof job === "string" ? { suite: job } : job;
}

/**
 * Whether stored ids and allow lists carry *job*: the suite directly or through
 * a former id, and the job's function kept by every allow list that narrows it.
 * A list keyed by a former id narrows that id only; a list keyed by the group
 * narrows the group.
 */
export function carriesJob(
  ids: readonly string[] | null | undefined,
  allowed: Record<string, string[] | null> | null | undefined,
  job: string | ToolJob,
  aliases: AliasMap = NO_ALIASES,
): boolean {
  const { suite, fn } = asJob(job);
  const via = (ids ?? []).filter((id) => canonicalSuiteId(id, aliases) === suite);
  if (via.length === 0) return false;
  if (!fn) return true;
  const groupList = allowed?.[suite];
  if (groupList && !groupList.includes(fn)) return false;
  // A former id with its own list keeps the function only if that list does;
  // one id that keeps it (or has no list) is enough.
  return via.some((id) => id === suite || !allowed?.[id] || allowed[id]!.includes(fn));
}

/** The enabled chat agents carrying *job*, by name. */
export function agentsCarrying(agents: readonly Agent[], job: string | ToolJob, aliases: AliasMap = NO_ALIASES): Agent[] {
  return agents
    .filter((a) => a.enabled && !a.disable_chat && carriesJob(a.builtin_tool_ids, a.builtin_allowed_tools, job, aliases))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The agent offered first among *toolAgents* - `null` when the answer is the
 * General Assistant (or nobody). The user's own default agent when it carries
 * the tool (their chat opens on it anyway); else the General Assistant when
 * offered - it carries the tool out of the box and its persona is general,
 * whereas the built-in agents carrying it are specialists; else a
 * company-managed agent carrying it; else any.
 */
export function firstToolAgent(
  toolAgents: readonly Agent[],
  explicitDefaultId: string | null,
  generalAssistantOffered: boolean,
): string | null {
  const explicit = explicitDefaultId ? toolAgents.find((a) => a.id === explicitDefaultId) : undefined;
  if (explicit) return explicit.id;
  if (generalAssistantOffered) return null;
  return (toolAgents.find((a) => a.is_company_managed) ?? toolAgents[0])?.id ?? null;
}
