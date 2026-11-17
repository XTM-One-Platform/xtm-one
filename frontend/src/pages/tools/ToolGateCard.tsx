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
 * Whether a tool asks first, on the tool's own page.
 *
 * A statement and a way to the control, never the control itself: the
 * platform's rule is changed in Settings > Access and security > Tool approvals and a
 * person's own approval in their profile, so the tool page links to the right
 * one for the reader instead of carrying both decisions beside each other.
 *
 * Renders nothing while approvals are not in use (no agent asks before running
 * tools, or approvals are suspended), and nothing while the catalogue has no
 * entry for these names: an MCP or integration tool that has never run is
 * genuinely unknown here.
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useFormatter } from "@/i18n";
import { useToolGateCatalog } from "@/hooks/api/useHitlWhitelist";
import { useApprovalsInUse, useApprovedToolKeys, useToolApprovals } from "@/hooks/api/useToolApprovals";
import { useAuth } from "@/hooks/useAuth";
import { useExemptReason, useGateReason } from "@/hooks/useHitlWords";
import { hitlState, onPersonalList, toolApprovalsHref } from "@/lib/hitl";
import { cn } from "@/lib/utils";

export function ToolGateCard({
  toolKeys,
  group,
  affectsOthers,
}: {
  /** Exact tool names to report. Ignored when `group` is given. */
  toolKeys?: string[];
  /** A builtin id — reports every tool the catalogue attributes to it. */
  group?: string;
  /**
   * Other people can call this tool, so the platform rule reaches their runs
   * too. For a shared or company-managed custom tool.
   */
  affectsOthers?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const inUse = useApprovalsInUse();
  const { data } = useToolGateCatalog();
  const { data: mine } = useToolApprovals();
  const reason = useExemptReason();
  const gateReason = useGateReason();

  const entries = useMemo(() => {
    const all = data?.tools ?? [];
    if (group) return all.filter((t) => t.group === group);
    const wanted = new Set(toolKeys ?? []);
    return all.filter((t) => wanted.has(t.tool_key));
  }, [data?.tools, group, toolKeys]);

  const approved = useApprovedToolKeys();

  if (!inUse || entries.length === 0) return null;

  const allowAll = mine?.approve_all_tools ?? false;
  const asksYou = (toolKey: string) => !allowAll && !approved.has(toolKey);
  const asking = entries.filter((e) => e.gates).length;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <HitlIcon state="topic" className="size-4" decorative />
          {t_i18n("Approval")}
        </CardTitle>
        <CardDescription className="content-compact">
          {entries.length === 1
            ? asking > 0
              ? t_i18n("On an agent that asks before running tools, this tool waits for a person to approve it.")
              : t_i18n("This tool runs without asking, on every agent.")
            : t_i18n("{asking} of {total} of its tools ask first, on an agent that asks before running tools.", {
                asking,
                total: entries.length,
              })}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 pt-0">
        {allowAll && asking > 0 && (
          <p className="text-[11px] text-default-secondary">
            {t_i18n("You allow every tool, so it does not ask you.")}
          </p>
        )}
        {isAdmin && affectsOthers && (
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "This tool is shared, so a platform rule for it applies to everyone who can use it, scheduled runs included.",
            )}
          </p>
        )}
        <ul className="space-y-1">
          {entries.map((entry) => (
            <li
              key={entry.tool_key}
              className="flex items-center gap-2.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2"
            >
              <HitlIcon
                state={hitlState(entry.gates)}
                className={cn(!entry.gates && "text-default-secondary")}
                decorative
              />
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono-plex content-compact">{entry.tool_key}</p>
                <p className="truncate text-[11px] text-default-secondary">
                  {entry.gates && !asksYou(entry.tool_key)
                    ? t_i18n("Asks first; you always allow it")
                    : entry.gates
                    ? gateReason(entry)
                    : reason(entry)}
                </p>
              </div>
            </li>
          ))}
        </ul>
        {(isAdmin || entries.some((entry) => onPersonalList(entry, approved))) && (
          <Link
            to={toolApprovalsHref(entries.length === 1 ? entries[0].tool_key : undefined, isAdmin)}
            className="inline-flex items-center gap-1 text-[11px] text-accent underline-offset-2 hover:underline"
          >
            {isAdmin
              ? t_i18n("Change it in the platform's tool rules")
              : t_i18n("Manage your own approvals")}
            <Icon name="arrow-right" size={16} />
          </Link>
        )}
      </CardContent>
    </Card>
  );
}
