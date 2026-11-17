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

import { Fragment, useMemo } from "react";
import { Link } from "react-router-dom";
import { Wrench } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useBuiltinToolCatalog } from "@/hooks/api/useSettingsApi";
import { useToolGateCatalog } from "@/hooks/api/useHitlWhitelist";
import { useApprovalsInUse, useApprovedToolKeys } from "@/hooks/api/useToolApprovals";
import { useExemptReason, useGateReason } from "@/hooks/useHitlWords";
import { hitlState, toolApprovalLink } from "@/lib/hitl";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export function BuiltinToolsCard({ builtinId }: { builtinId: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { data: catalog } = useBuiltinToolCatalog();
  const { data: gates } = useToolGateCatalog();
  /* The Approval column only while approvals are in use: with no agent asking
     before running tools, "Asks first" on a row describes nothing anyone gets. */
  const inUse = useApprovalsInUse();
  const approved = useApprovedToolKeys();
  const reason = useExemptReason();
  const gateReason = useGateReason();
  const tools = catalog?.[builtinId] ?? [];

  const gateByName = useMemo(() => {
    const map = new Map<string, NonNullable<typeof gates>["tools"][number]>();
    for (const entry of gates?.tools ?? []) map.set(entry.tool_key, entry);
    return map;
  }, [gates]);

  if (!catalog) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Wrench className="h-4 w-4 text-accent" />
          {t_i18n("Available Tools")}
          <Badge variant="cyan" className="content-caption ml-auto">
            {t_i18n("{count, plural, one {# tool} other {# tools}}", { count: tools.length })}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {tools.length === 0 ? (
          <div className="text-center py-6">
            <Wrench className="h-8 w-8 text-default-disabled mx-auto mb-2" />
            <p className="content-base text-default-secondary">{t_i18n("No sub-tools exposed for this built-in tool.")}</p>
          </div>
        ) : (
          /* ONE grid for the whole table, not one per row: shared tracks are
             the only thing that aligns the cells of every row. Padding lives in
             the cells rather than in a column gap, so the row rule underneath
             stays continuous. */
          <div
            className={cn(
              "grid rounded-sm border border-elevation-subtle-soft overflow-hidden",
              inUse ? "grid-cols-[212px_1fr_auto]" : "grid-cols-[212px_1fr]",
            )}
          >
            {[t_i18n("Tool Name"), t_i18n("Description"), ...(inUse ? [t_i18n("Approval")] : [])].map(
              (label, i) => (
                <p
                  key={i}
                  className={cn(
                    "py-2 pr-4 content-caption font-semibold uppercase tracking-wider text-default-secondary bg-elevation-hover border-b border-elevation-subtle-soft",
                    i === 0 && "pl-4",
                  )}
                >
                  {label}
                </p>
              ),
            )}
            {tools.map((tool, idx) => {
              const gate = gateByName.get(tool.name);
              const href = gate ? toolApprovalLink(gate, isAdmin, approved) : null;
              const cell = cn(
                "flex items-center py-2.5 pr-4",
                idx !== tools.length - 1 && "border-b border-elevation-subtle-soft",
              );
              const badge = gate && (
                <Badge
                  variant="outline"
                  className={cn("text-[9px] w-fit gap-0.5 shrink-0", href && "hover:bg-elevation-hover")}
                >
                  <HitlIcon
                    state={hitlState(gate.gates)}
                    className={cn("size-2.5", !gate.gates && "text-default-secondary")}
                    decorative
                  />
                  {gate.gates ? t_i18n("Asks first") : t_i18n("Runs without asking")}
                </Badge>
              );
              return (
                <Fragment key={tool.name}>
                  <div className={cn(cell, "pl-4")}>
                    <Tip content={tool.name}>
                      <code className="rounded-sm px-1.5 py-0.5 text-[11px] font-mono-plex truncate block max-w-[180px] bg-feedback-success-primary/10 text-feedback-success-primary">
                        {tool.name}
                      </code>
                    </Tip>
                  </div>
                  <div className={cell}>
                    <span className="content-compact text-default-secondary leading-relaxed">
                      {tool.description}
                    </span>
                  </div>
                  {/* The state in words, linking to where it is changed: the
                      platform's rules for an administrator, the reader's own
                      approvals otherwise - unless their list has no row for
                      it. Empty rather than a guess when the catalogue does
                      not know this name. */}
                  {inUse && (
                    <div className={cn(cell, "justify-end")}>
                      {gate && (
                        <Tip content={gate.gates ? gateReason(gate) : reason(gate)}>
                          {href ? <Link to={href}>{badge}</Link> : badge}
                        </Tip>
                      )}
                    </div>
                  )}
                </Fragment>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
