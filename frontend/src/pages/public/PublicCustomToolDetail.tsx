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

import { Braces, ChevronDown, Clock, Code2, Wrench } from "lucide-react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { usePublicCatalogCustomTool } from "@/hooks/api/usePublicCatalogApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

import { CopyButton, CatalogDetailSkeleton, DetailShell } from "./components";

export default function PublicCustomToolDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const { data: tool, isLoading } = usePublicCatalogCustomTool(id!);
  const [codeExpanded, setCodeExpanded] = useState(false);

  if (isLoading) return <CatalogDetailSkeleton backTo="/catalog/tools" backLabel="Back to Tools" />;
  if (!tool)
    return (
      <p className="text-center text-default-secondary py-20">
        {t_i18n("Custom tool not found")}
      </p>
    );

  const paramCount = tool.input_schema
    ? Object.keys(tool.input_schema.properties ?? {}).length
    : 0;
  const isLongCode = (tool.js_function?.length ?? 0) > 600;

  return (
    <DetailShell
      backTo="/catalog/tools"
      backLabel="Back to Tools"
      entityType="custom-tools"
      entityId={tool.id}
      icon={<Wrench className="h-6 w-6 text-accent" />}
      name={tool.name}
      description={tool.description}
      tags={tool.tags}
      createdAt={tool.created_at}
      updatedAt={tool.updated_at}
      sharedBy={tool.shared_by}
    >
      {/* Quick stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4 text-center">
          <Code2 className="h-4 w-4 text-accent mx-auto mb-1" />
          <p className="text-lg font-bold">{tool.js_function?.split("\n").length ?? 0}</p>
          <p className="content-caption text-default-secondary">{t_i18n("lines of code")}</p>
        </div>
        <div className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4 text-center">
          <Braces className="h-4 w-4 text-accent mx-auto mb-1" />
          <p className="text-lg font-bold">{paramCount}</p>
          <p className="content-caption text-default-secondary">{t_i18n("{count, plural, one {parameter} other {parameters}}", { count: paramCount })}</p>
        </div>
        <div className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4 text-center">
          <Badge variant="cyan" className="content-caption mx-auto">
            {tool.language === "python" ? "Python" : "JavaScript"}
          </Badge>
          <p className="content-caption text-default-secondary mt-1.5">{t_i18n("runtime")}</p>
        </div>
        <div className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4 text-center">
          <Clock className="h-4 w-4 text-accent mx-auto mb-1" />
          <p className="text-lg font-bold">
            {(tool.timeout ?? 300) >= 60
              ? `${Math.floor((tool.timeout ?? 300) / 60)}m`
              : `${tool.timeout ?? 300}s`}
          </p>
          <p className="content-caption text-default-secondary">{t_i18n("timeout")}</p>
        </div>
      </div>

      {/* Input schema */}
      {tool.input_schema && paramCount > 0 && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-3">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
            {t_i18n("Input Parameters")}
          </h2>
          <div className="divide-y divide-[var(--border-elevation-subtle)]">
            {Object.entries(tool.input_schema.properties ?? {}).map(([name, schema]) => (
              <div key={name} className="py-2.5 first:pt-0 last:pb-0">
                <div className="flex items-center gap-2">
                  <span className="content-base font-medium font-mono-plex text-accent">{name}</span>
                  <Badge variant="outline" className="text-[9px]">{schema.type}</Badge>
                  {(tool.input_schema!.required ?? []).includes(name) && (
                    <Badge variant="outline" className="text-[9px] text-feedback-warning-primary border-feedback-alert-primary/20">{t_i18n("required")}</Badge>
                  )}
                </div>
                {schema.description && (
                  <p className="content-compact text-default-secondary mt-0.5">{schema.description}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* JavaScript function */}
      {tool.js_function && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-2">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <div className="flex items-center justify-between">
            <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
              {t_i18n("JavaScript Function")}
            </h2>
            <CopyButton text={tool.js_function} label="Copy Code" />
          </div>
          <div
            className={cn(
              "relative",
              !codeExpanded && isLongCode && "overflow-hidden",
            )}
            style={!codeExpanded && isLongCode ? { maxHeight: 250 } : undefined}
          >
            <pre className="overflow-auto rounded-sm bg-elevation-default p-4 content-compact font-mono-plex leading-relaxed border border-elevation-subtle-soft text-accent/80">
              {tool.js_function}
            </pre>
            {!codeExpanded && isLongCode && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent" />
            )}
          </div>
          {isLongCode && (
            <button
              onClick={() => setCodeExpanded(!codeExpanded)}
              className="flex items-center gap-1 content-compact text-accent hover:text-accent/80 transition-colors"
            >
              <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", codeExpanded && "rotate-180")} />
              {codeExpanded ? t_i18n("Collapse") : t_i18n("Expand full code")}
            </button>
          )}
        </div>
      )}
    </DetailShell>
  );
}
