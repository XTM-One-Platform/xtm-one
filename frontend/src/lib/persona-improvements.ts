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
 * The persona self-improvement loop in the UI, without React.
 */
import type { ImprovementProposal, ImprovementProposalList, ImprovementProposalStatus } from "@/types";

/** Applied, and still being judged: the scheduler may yet roll it back. */
const IN_FLIGHT: ReadonlySet<ImprovementProposalStatus> = new Set(["applying", "shadow", "watching"]);

/** A persona rewrite a person asked for: the agent's list also holds the
 *  Concierge's proposals (any type), which the inbox shows, not this card. */
export function isPersonaRewrite(item: Pick<ImprovementProposal, "proposal_type" | "author_agent_id">): boolean {
  return item.proposal_type === "persona" && item.author_agent_id === null;
}

export function inFlight(status: ImprovementProposalStatus): boolean {
  return IN_FLIGHT.has(status);
}

/** Whether a change being judged in *before* is settled in *after*: the
 *  scheduler may have rolled the persona back, which only the agent's own
 *  queries show. */
export function cycleSettled(
  before: ImprovementProposalList | undefined,
  after: ImprovementProposalList,
): boolean {
  if (!before) return false;
  const status = new Map(after.items.map((item) => [item.id, item.status]));
  return before.items.some((item) => inFlight(item.status) && !inFlight(status.get(item.id) ?? "failed"));
}
