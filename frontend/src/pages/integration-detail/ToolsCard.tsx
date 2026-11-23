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
import { Link } from "react-router-dom";
import { Wrench, BookOpen, Pencil, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { Tip } from "@/components/ui/tooltip";
import { ToolTestDialog } from "@/components/ui/tool-test-dialog";
import { useIntegrationToolList, useIntegrationTools, useTestIntegrationTool } from "@/hooks/api/useIntegrationApi";
import { useToolGateCatalog, type ToolGateEntry } from "@/hooks/api/useHitlWhitelist";
import { useApprovalsInUse, useApprovedToolKeys } from "@/hooks/api/useToolApprovals";
import { useAuth } from "@/hooks/useAuth";
import { useExemptReason, useGateReason } from "@/hooks/useHitlWords";
import { hitlState, toolApprovalLink } from "@/lib/hitl";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Integration, JsonSchema, ProviderCatalogItem } from "@/types";

export function ToolsCard({ catalogEntry, integration }: { catalogEntry: ProviderCatalogItem; integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  // Catalog-declared tools, or — for a provider whose tools come from the
  // integration's own imported spec (OpenAPI) — the integration's tools.
  const tools = useIntegrationToolList(integration, catalogEntry);
  const { data: liveToolsData } = useIntegrationTools(integration.id);
  /* The Approval column only while approvals are in use (an agent asks before
     running tools): otherwise every tool here runs without asking, whatever
     the column would say. */
  const inUse = useApprovalsInUse();
  const approved = useApprovedToolKeys();
  const columns = inUse ? "grid-cols-[180px_1fr_80px_112px_50px]" : "grid-cols-[180px_1fr_80px_50px]";
  /* Read/write is already on every row below, and that
     classification is what decides whether a tool pauses — reads of a
     connected provider are exempt by derivation. So the table showed the
     input to the rule and hid its result, and the exceptions are exactly
     where it matters: `download_opencti_file` classifies as a read and is
     hand-gated anyway. */
  const { data: gateCatalog } = useToolGateCatalog();
  const reason = useExemptReason();
  const gateReason = useGateReason();
  const gateByName = useMemo(() => {
    const map = new Map<string, ToolGateEntry>();
    for (const entry of gateCatalog?.tools ?? []) map.set(entry.tool_key, entry);
    return map;
  }, [gateCatalog?.tools]);
  const testTool = useTestIntegrationTool();
  const [testingToolName, setTestingToolName] = useState<string | null>(null);
  const isConnected = integration.status === "connected" || integration.status === "configured";

  if (!tools || tools.length === 0) return null;

  const readTools = tools.filter((t) => t.tool_type === "read");
  const writeTools = tools.filter((t) => t.tool_type === "write");

  // Build a map of live tool schemas (name -> input_schema) for the test dialog
  const schemaMap = new Map<string, JsonSchema | null>();
  for (const lt of liveToolsData?.tools ?? []) {
    schemaMap.set(lt.name, lt.input_schema ?? null);
  }

  const testingTool = testingToolName ? tools.find((t) => t.name === testingToolName) : null;

  const ToolRow = ({ tool, color, isLast }: { tool: { name: string; description: string; tool_type: string }; color: "emerald" | "amber"; isLast: boolean }) => {
    const gate = gateByName.get(tool.name);
    /* Per connection, as the bot dialog does. The catalogue calls an
       integration's tool exempt only when EVERY connection of the provider is
       switched off — the honest summary for a list of names, and the wrong one
       on one connection's page, where `ConfigurationCard` right above says
       "its tools run without asking" from this connection's own switch. Two
       halves of one screen contradicting each other is worse than either. */
    const gatesHere = gate ? gate.gates && integration.requires_approval : undefined;
    const href = gate ? toolApprovalLink(gate, isAdmin, approved) : null;
    const badge = (
      /* The same chip as the Type column beside it: an outline badge with a
         tinted icon. Two adjacent columns carrying the same shape of fact
         should not be styled one as a badge and one as loose text — the
         difference reads as meaning where there is none. The tint is the
         shield's own, as everywhere else. */
      <Badge variant="outline" className={cn("text-[9px] w-fit gap-0.5", href && "hover:bg-elevation-hover")}>
        <HitlIcon
          state={hitlState(gatesHere)}
          className={cn("size-2.5", !gatesHere && "text-default-secondary")}
          decorative
        />
        {gatesHere ? t_i18n("Asks first") : t_i18n("Runs without asking")}
      </Badge>
    );
    return (
    <div key={tool.name} className={cn("grid gap-4 px-4 py-2.5 items-center group", columns, !isLast ? "border-b border-elevation-subtle-soft" : "")}>
      <Tip content={tool.name}>
        <code className={cn("rounded-sm px-1.5 py-0.5 content-caption font-mono-plex truncate block max-w-[180px]", color === "emerald" ? "bg-feedback-success-primary/10 text-feedback-success-primary" : "bg-feedback-alert-primary/10 text-feedback-warning-tertiary")}>
          {tool.name}
        </code>
      </Tip>
      <span className="content-compact text-default-secondary leading-relaxed">{tool.description}</span>
      <Badge variant="outline" className="text-[9px] w-fit">
        {color === "emerald" ? <BookOpen className="h-2.5 w-2.5 mr-0.5 text-feedback-success-primary" /> : <Pencil className="h-2.5 w-2.5 mr-0.5 text-feedback-warning-primary" />}
        {tool.tool_type === "read" ? t_i18n("Read") : t_i18n("Write")}
      </Badge>
      {/* Deployment-wide, not this connection alone: the rules answer by
          name, so a provider with several connections gets one answer for all
          of them.

          A link rather than a note saying where to go: the platform rules
          live in Settings (a person's own approvals in their profile), and
          next to this connection's own switch they would read as changing
          this connection. `?tool=` lands the reader on this one row. A
          non-admin's list holds no row for a tool that runs without asking
          them, so that one is the chip alone. */}
      {!inUse ? null : gate ? (
        /* The whole cell is the trigger, not the 14px shield inside it: the
           words beside the icon are what a reader aims at, and a tooltip they
           have to hit a glyph to see is one most people never read. The icon
           is `decorative` here for that reason — a tooltip of its own would
           nest a second trigger inside this one, and the chip's visible text
           already names the state. */
        <Tip
          content={`${
            gatesHere
              ? gateReason(gate)
              : gate.gates
              ? t_i18n("This connection is set to run its tools without asking.")
              : reason(gate)
          }${
            href === null
              ? ""
              : ` ${
                  isAdmin
                    ? t_i18n("Change it in Settings > Access and security > Tool approvals.")
                    : t_i18n("Your own approvals are in your profile.")
                }`
          }`}
        >
          {href ? <Link to={href}>{badge}</Link> : badge}
        </Tip>
      ) : (
        /* Not a link: nothing here answers for this name, so there is no row
           to land on. The shield keeps its own tooltip, which says so. */
        <Badge variant="outline" className="text-[9px] w-fit gap-0.5">
          <HitlIcon state="unknown" className="size-2.5" />—
        </Badge>
      )}
      {isConnected && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 gap-1 content-caption px-1.5 opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={() => setTestingToolName(tool.name)}
        >
          {t_i18n("Test")}
        </Button>
      )}
    </div>
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Wrench className="h-4 w-4 text-accent" />
          {t_i18n("Available Tools")}
          <Badge variant="cyan" className="content-caption ml-auto">{t_i18n("{count} tools", { count: tools.length })}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-sm bg-elevation-hover p-3">
          <div className="flex items-start gap-2">
            <Info className="h-3.5 w-3.5 shrink-0 text-accent mt-0.5" />
            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("These tools are automatically available to any agent that has this integration bound.")}
              {isConnected && ` ${t_i18n("Hover a tool and click Test to try it with real credentials.")}`}
            </p>
          </div>
        </div>

        {/* Full-width table layout */}
        <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
          {/* Header */}
          <div className={cn("grid gap-4 px-4 py-2 bg-elevation-hover border-b border-elevation-subtle-soft", columns)}>
            <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Tool Name")}</p>
            <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Description")}</p>
            <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Type")}</p>
            {inUse && (
              <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Approval")}</p>
            )}
            <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary"></p>
          </div>

          {readTools.length > 0 && (
            <>
              <div className="px-4 py-1.5 bg-feedback-success-primary/[0.03] border-b border-elevation-subtle-soft">
                <div className="flex items-center gap-1.5">
                  <BookOpen className="h-3 w-3 text-feedback-success-primary" />
                  <h4 className="content-caption font-semibold uppercase tracking-wider text-feedback-success-primary">{t_i18n("Read Tools")}</h4>
                  <CountBadge variant="outline" className="ml-1">{readTools.length}</CountBadge>
                </div>
              </div>
              {readTools.map((tool, idx) => (
                <ToolRow key={tool.name} tool={tool} color="emerald" isLast={idx === readTools.length - 1 && writeTools.length === 0} />
              ))}
            </>
          )}

          {writeTools.length > 0 && (
            <>
              <div className="px-4 py-1.5 bg-feedback-alert-primary/[0.03] border-b border-elevation-subtle-soft">
                <div className="flex items-center gap-1.5">
                  <Pencil className="h-3 w-3 text-feedback-warning-primary" />
                  <h4 className="content-caption font-semibold uppercase tracking-wider text-feedback-warning-primary">{t_i18n("Write Tools")}</h4>
                  <CountBadge variant="outline" className="ml-1">{writeTools.length}</CountBadge>
                </div>
              </div>
              {writeTools.map((tool, idx) => (
                <ToolRow key={tool.name} tool={tool} color="amber" isLast={idx === writeTools.length - 1} />
              ))}
            </>
          )}
        </div>
      </CardContent>

      {/* Tool test dialog */}
      <ToolTestDialog
        open={testingToolName !== null}
        onOpenChange={(open) => { if (!open) setTestingToolName(null); }}
        toolName={testingToolName ?? ""}
        toolDescription={testingTool?.description}
        inputSchema={testingToolName ? (schemaMap.get(testingToolName) ?? null) : null}
        onRun={async (args) => {
          return new Promise((resolve) => {
            testTool.mutate(
              { integrationId: integration.id, tool_name: testingToolName ?? "", arguments: args },
              {
                onSuccess: (data) => resolve(data),
                onError: (err) => resolve({ status: "error", message: err.message }),
              },
            );
          });
        }}
      />
    </Card>
  );
}
