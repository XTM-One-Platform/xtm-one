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
 * The owner's account is deactivated: nothing starts this assignment
 * or flow unless it is company-managed, whatever its own switch says.
 * The pause is derived server-side and never stored, so there is no resume
 * action here - an admin gets the owner's page instead, where the account is
 * reactivated, and for a flow the builder's Owner and access drawer, where it
 * is transferred or made company-managed.
 */

import { UserCog, UserRound, UserX } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { IconMark } from "@/components/ui/icon-mark";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

type PausedEntity = "assignment" | "flow";

/**
 * How an administrator keeps a paused automation running while the account
 * stays disabled: an owner transfer (an assignment follows its agent's owner)
 * or making it company-managed, the one kind that does not pause.
 */
export function useOwnerDeactivatedRemedy(entity: PausedEntity): string {
  const { t_i18n } = useFormatter();
  return entity === "flow"
    ? t_i18n("An administrator can keep it running by transferring the flow to an active owner or making it company-managed.")
    : t_i18n("An administrator can keep it running by transferring its agent to an active owner or making the agent company-managed.");
}

interface OwnerDeactivatedNoticeProps {
  entity: PausedEntity;
  ownerId: string | null;
  /** What is held and when it resumes, worded for the entity. */
  description: string;
  /** Tooltip of the admin's link to the owner's account. */
  reactivateHint: string;
  /** Admin link that opens where the remedy is applied (flows: the builder's Owner and access drawer). */
  ownerAccessHref?: string;
}

export function OwnerDeactivatedNotice({ entity, ownerId, description, reactivateHint, ownerAccessHref }: OwnerDeactivatedNoticeProps) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const remedy = useOwnerDeactivatedRemedy(entity);

  return (
    <div className="rounded-sm p-3 flex items-start gap-2.5 bg-feedback-alert-primary/5">
      <UserX className="h-4 w-4 shrink-0 mt-0.5 text-feedback-warning-primary" />
      <div className="space-y-1 flex-1 min-w-0">
        <p className="content-compact font-medium leading-relaxed text-feedback-warning-primary">
          {t_i18n("Paused: the owner's account is deactivated")}
        </p>
        <p className="content-compact leading-relaxed text-feedback-warning-primary">{description}</p>
        <p className="content-compact leading-relaxed text-feedback-warning-primary">{remedy}</p>
      </div>
      {isAdmin && (ownerId || ownerAccessHref) && (
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          {ownerAccessHref && (
            <Button asChild variant="outline" size="sm" className="h-7 gap-1.5 content-compact">
              <Link to={ownerAccessHref}>
                <UserCog className="h-3 w-3" />
                {t_i18n("Owner and access")}
              </Link>
            </Button>
          )}
          {ownerId && (
            <Tip content={reactivateHint}>
              <Button asChild variant="outline" size="sm" className="h-7 gap-1.5 content-compact">
                <Link to={`/users-and-groups/users/${ownerId}`}>
                  <UserRound className="h-3 w-3" />
                  {t_i18n("Open the owner's account")}
                </Link>
              </Button>
            </Tip>
          )}
        </div>
      )}
    </div>
  );
}

/** The reason, then how an administrator keeps it running meanwhile. */
function PauseTipContent({ entity }: { entity: PausedEntity }) {
  const { t_i18n } = useFormatter();
  const remedy = useOwnerDeactivatedRemedy(entity);
  return (
    <div className="space-y-1">
      <p>{t_i18n("Paused: the owner's account is deactivated")}</p>
      <p>{remedy}</p>
    </div>
  );
}

/** The compact form, for a row or a toolbar. */
export function OwnerDeactivatedBadge({ entity, className }: { entity: PausedEntity; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={<PauseTipContent entity={entity} />}>
      <Badge variant="outline" className={cn("shrink-0 gap-1 border-feedback-alert-primary/40 content-caption text-feedback-warning-primary", className)}>
        <UserX className="h-2.5 w-2.5" />
        {t_i18n("Paused")}
      </Badge>
    </Tip>
  );
}

/** Icon-only form, for a card's icon row: a tab stop through `IconMark`. */
export function OwnerDeactivatedIcon({ entity }: { entity: PausedEntity }) {
  const { t_i18n } = useFormatter();
  return (
    <IconMark label={t_i18n("Paused: the owner's account is deactivated")} tip={<PauseTipContent entity={entity} />}>
      <UserX className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" aria-hidden="true" />
    </IconMark>
  );
}
