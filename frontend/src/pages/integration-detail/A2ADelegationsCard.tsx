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

/** What this outbound partner has been asked to do.
 *
 * The activity view for an outbound registration. Deliberately NOT the shared
 * `EntityActivityTab`: that lists RUNS, and a delegation is a tool call inside
 * a run rather than a run of its own. Showing the runs of every agent bound to
 * this integration would answer a different question — "what did these agents
 * do", most of it nothing to do with this partner.
 *
 * Scoped by the registration recorded on each delegation at submission time.
 * Two registrations may be bound to one agent, so filtering by binding would
 * make each display the other's traffic.
 */

import { ArrowUpRight, CheckCircle2, XCircle, Clock, ShieldAlert } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatShortDateTime } from "@/lib/utils";
import { useA2ADelegations } from "@/hooks/api/useA2AClientApi";
import type { Integration } from "@/types";

export function A2ADelegationsCard({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { data: delegations, isLoading } = useA2ADelegations(integration.id);

  // 404 for an inbound registration or a deployment with the feature off.
  if (isLoading || !delegations) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <ArrowUpRight className="h-4 w-4 text-accent" />
          {t_i18n("Work sent to this partner")}
        </CardTitle>
      </CardHeader>
      <CardContent className="content-compact">
        {delegations.length === 0 ? (
          <p className="text-default-secondary">
            {t_i18n(
              "Nothing sent yet. Bind this integration to an agent on its Tools tab to give that agent a delegation tool.",
            )}
          </p>
        ) : (
          // Capped and scrollable: the endpoint returns up to 50, each row is
          // two lines, and an active registration would otherwise push every
          // card below it off the page. ``-mr-2 pr-2`` keeps the scrollbar off
          // the text without inheriting the card's padding.
          <ul className="max-h-72 overflow-y-auto space-y-2 -mr-2 pr-2">
            {delegations.map((d) => (
              <li key={d.id} className="flex items-start gap-2">
                <OutcomeIcon outcome={d.outcome} />
                <div className="min-w-0 flex-1">
                  <p className="truncate">
                    {d.agent_name ?? t_i18n("A deleted agent")}
                    {d.continuation_count > 0 && (
                      <span className="text-default-secondary">
                        {" "}
                        · {t_i18n("with follow-up")}
                      </span>
                    )}
                  </p>
                  <p className="content-caption text-default-secondary">
                    {formatShortDateTime(d.submitted_at)} · A2A{" "}
                    {d.protocol_version}
                    {/* Provenance per delegation, not per registration: what
                        this one ran under is what mattered for this one. */}
                    {d.signature_state === "unsigned" && (
                      <Tip content={t_i18n("This delegation ran against an unsigned partner card.")}>
                        <span className="inline-flex items-center gap-0.5 text-feedback-warning-primary">
                          {" "}
                          <ShieldAlert className="h-3 w-3" />
                        </span>
                      </Tip>
                    )}
                  </p>
                  {d.partner_reason && (
                    <p className="content-caption text-default-secondary truncate">
                      {d.partner_reason}
                    </p>
                  )}
                </div>
                {d.outcome && (
                  <Badge variant="outline" className="content-caption shrink-0">
                    {d.outcome}
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function OutcomeIcon({ outcome }: { outcome?: string | null }) {
  if (outcome === "completed")
    return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 mt-0.5 text-accent" />;
  if (outcome === "timeout")
    return <Clock className="h-3.5 w-3.5 shrink-0 mt-0.5 text-feedback-warning-primary" />;
  return <XCircle className="h-3.5 w-3.5 shrink-0 mt-0.5 text-feedback-error-primary" />;
}
