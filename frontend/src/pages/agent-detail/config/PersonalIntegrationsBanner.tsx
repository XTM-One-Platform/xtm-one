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

import { useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, RotateCcw, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { resolvesPerUser } from "@/lib/utils";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Agent, Integration } from "@/types";
import {
  bannerPersonalIntegrations,
  lendsAll,
  type IntegrationLending,
  type PersonalIntegration,
} from "./personal-integrations";

type Props = {
  agent: Pick<Agent, "user_id" | "owner_name" | "integration_bindings"> & {
    integration_lending?: IntegrationLending | null;
    shared_personal_integrations?: PersonalIntegration[] | null;
  };
  integrations?: Integration[];
  sharedGroupIds: string[] | null;
  sharedUserIds?: string[] | null;
  currentUserId?: string;
  disabled?: boolean;
  /** Lend these integration ids through the agent; `[]` stops lending. */
  onLend?: (integrationIds: string[]) => void;
  onMakeBindingsPerUser?: (integrationIds: string[]) => void;
};

/**
 * What the people an agent is shared with get from its owner's personal
 * integrations when they chat with it. Once the owner lends them through the
 * agent the warning collapses to one green line; "Review" reopens it with
 * every choice, including stopping the lending.
 */
export function PersonalIntegrationsBanner({
  agent,
  integrations,
  sharedGroupIds,
  sharedUserIds = null,
  currentUserId,
  disabled,
  onLend,
  onMakeBindingsPerUser,
}: Props) {
  const { t_i18n, intl } = useFormatter();
  const [reviewing, setReviewing] = useState(false);
  const personal = useMemo(
    () =>
      bannerPersonalIntegrations(
        agent.integration_bindings,
        integrations,
        sharedGroupIds,
        sharedUserIds,
        agent.shared_personal_integrations,
      ),
    [agent.integration_bindings, integrations, sharedGroupIds, sharedUserIds, agent.shared_personal_integrations],
  );
  if (personal.length === 0) return null;

  const ids = personal.map((i) => i.id);
  const lending = agent.integration_lending ?? null;
  const lent = lendsAll(lending, ids);
  const isOwner = !!currentUserId && currentUserId === agent.user_id;
  const label = (i: PersonalIntegration) =>
    resolvesPerUser(i.provider) ? (INTEGRATION_PROVIDER_NAMES[i.provider] ?? i.name) : i.name;
  const perUserConvertible = personal.filter((i) => resolvesPerUser(i.provider));
  const ownApi = personal.filter((i) => !resolvesPerUser(i.provider));

  if (lent && lending && !reviewing) {
    return (
      <div className="flex items-center gap-2 px-1 py-1" data-testid="personal-integrations-lent">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-feedback-success-primary" />
        <p className="min-w-0 flex-1 content-compact text-default-secondary">
          {t_i18n("Personal integrations used through this agent: {count}, set by {owner} on {date}", {
            count: personal.length,
            owner: agent.owner_name ?? t_i18n("the owner"),
            date: intl.formatDate(lending.set_at, { month: "short", day: "numeric" }),
          })}
        </p>
        {isOwner && (
          <Button variant="link" size="sm" className="h-6 content-compact" onClick={() => setReviewing(true)}>
            {t_i18n("Review")}
          </Button>
        )}
      </div>
    );
  }

  return (
    <div
      className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3 space-y-2.5"
      data-testid="personal-integrations-banner"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 text-feedback-warning-primary mt-0.5 shrink-0" />
        <div className="min-w-0 space-y-1">
          <p className="content-base font-medium text-feedback-warning-primary">{t_i18n("Personal integrations on a shared agent")}</p>
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "This agent is bound to {count, plural, one {# personal integration} other {# personal integrations}} of its owner ({names}). Its assignments, flows, web widget and channels act with {count, plural, one {it} other {them}} for everyone. Choose what the people it is shared with get when they chat with it:",
              { count: personal.length, names: personal.map(label).join(", ") },
            )}
          </p>
        </div>
      </div>
      <ul className="space-y-2 pl-6">
        <li className="space-y-1">
          <p className="text-[11px] text-default-secondary">
            <span className="font-medium text-default-primary">{t_i18n("Use my integrations through this agent:")} </span>
            {t_i18n(
              "the agent runs with these connected accounts in their chats too, under its persona, guardrails and tool scoping. They never get these integrations in their own chats or agents, and never see the credential.",
            )}
          </p>
          {lent ? (
            <Button
              variant="outline"
              size="sm"
              className="h-7 content-compact"
              disabled={disabled || !isOwner || !onLend}
              onClick={() => {
                onLend?.([]);
                setReviewing(false);
              }}
            >
              {t_i18n("Stop using my integrations through this agent")}
            </Button>
          ) : isOwner && onLend ? (
            <Button
              variant="outline"
              size="sm"
              className="h-7 content-compact gap-1.5 border-accent/40 text-accent hover:bg-accent/10"
              disabled={disabled}
              onClick={() => {
                onLend(ids);
                setReviewing(false);
              }}
            >
              <Users className="h-3 w-3" />
              {t_i18n("Use my integrations through this agent")}
            </Button>
          ) : (
            <p className="text-[11px] text-default-secondary">{t_i18n("Only the agent's owner can choose to use their integrations through it.")}</p>
          )}
        </li>
        {perUserConvertible.length > 0 && (
          <li className="space-y-1">
            <p className="text-[11px] text-default-secondary">
              <span className="font-medium text-default-primary">{t_i18n("Switch to per-user:")} </span>
              {t_i18n(
                "in a chat, each member uses their own connected integration of the same provider; the owner's stays for assignments and flows. A member with none gets none of its tools.",
              )}
            </p>
            {onMakeBindingsPerUser && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 content-compact gap-1.5"
                disabled={disabled}
                onClick={() => onMakeBindingsPerUser(perUserConvertible.map((i) => i.id))}
              >
                <RotateCcw className="h-3 w-3" />
                {t_i18n("Switch to per-user")}
              </Button>
            )}
          </li>
        )}
        {ownApi.length > 0 && (
          <li>
            <p className="text-[11px] text-default-secondary">
              {t_i18n(
                "{names}: an OpenAPI integration is its own API and has no per-user form. Use it through this agent, or members get none of its tools in their chats.",
                { names: ownApi.map((i) => i.name).join(", ") },
              )}
            </p>
          </li>
        )}
      </ul>
      {lent && (
        <div className="pl-6">
          <Button variant="link" size="sm" className="h-6 content-compact" onClick={() => setReviewing(false)}>
            {t_i18n("Hide")}
          </Button>
        </div>
      )}
    </div>
  );
}
