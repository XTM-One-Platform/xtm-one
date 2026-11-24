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

/** The agent card URLs this inbound registration publishes.
 *
 * The one thing a partner needs from us, and the only per-AGENT fact in an
 * otherwise per-registration setup — the credential and the pins below are
 * shared by every agent bound here. Listed together so an operator handing a
 * partner their details reads it all off one page.
 *
 * One query per row: there is no exposure listing for an integration, only
 * ``GET /agents/{id}/a2a``, so each row asks for its own and leans on the
 * query cache.
 *
 * Every bound agent gets a row (#11786): one with no card says why, so the
 * count in the title never exceeds what is listed.
 */

import { useState } from "react";
import { Check, Copy, Link2 } from "lucide-react";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import type { Agent, Integration } from "@/types";
import { useBoundAgentCards, type CardRowState } from "./a2a-card-rows";

function CardUrlRow({ agent, state }: { agent: Agent; state: CardRowState }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);

  const name = (
    <span className="w-32 shrink-0 truncate content-compact font-medium" title={agent.name}>
      {agent.name}
    </span>
  );

  if (state.kind !== "card") {
    const why =
      state.kind === "loading"
        ? t_i18n("Loading…")
        : state.kind === "hidden"
          ? t_i18n("You cannot manage this agent, so its card is not shown to you.")
          : state.reason === "disabled"
            ? t_i18n("Not exposed: the agent is disabled. Enable it to publish its card.")
            : state.reason === "no_owner"
              ? t_i18n("Not exposed: the agent has no owner for partner work to run as.")
              : t_i18n("Not exposed yet: the platform publishes this card at its next start. Unbind and bind this integration on the agent to publish it now.");
    return (
      <div className="flex items-center gap-2">
        {name}
        <span className="min-w-0 flex-1 content-caption text-default-secondary">{why}</span>
      </div>
    );
  }

  const copy = () => {
    navigator.clipboard.writeText(state.cardUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="flex items-center gap-2">
      {name}
      <code className="min-w-0 flex-1 truncate rounded-sm bg-elevation-highlight px-2 py-1 content-caption">
        {state.cardUrl}
      </code>
      <Button size="sm" variant="outline" className="h-7 shrink-0 gap-1 px-2" onClick={copy} aria-label={t_i18n("Copy")}>
        {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      </Button>
    </div>
  );
}

export function A2ACardUrlsCard({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { bound, states } = useBoundAgentCards(integration.id);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Link2 className="h-4 w-4 text-accent" />
          {t_i18n("Agent card URLs")}
          <CountBadge variant="cyan" className="ml-auto">{bound.length}</CountBadge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {bound.length === 0 ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("Bind this integration to an agent to publish a card the partner can fetch.")}
          </p>
        ) : (
          <>
            {bound.map((agent, i) => (
              <CardUrlRow key={agent.id} agent={agent} state={states[i]} />
            ))}
            <p className="content-caption text-default-secondary">
              {t_i18n("One card per exposed agent. The credential below is shared by all of them.")}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
