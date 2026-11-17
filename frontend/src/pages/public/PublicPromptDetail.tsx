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

import { ChevronDown, FileText } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useParams } from "react-router-dom";

import { usePublicCatalogPrompt } from "@/hooks/api/usePublicCatalogApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

import { CopyButton, CatalogDetailSkeleton, DetailShell } from "./components";

const COLLAPSED_HEIGHT = 300;

export default function PublicPromptDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const { data: prompt, isLoading } = usePublicCatalogPrompt(id!);
  const [expanded, setExpanded] = useState(false);

  if (isLoading) return <CatalogDetailSkeleton backTo="/catalog/prompts" backLabel="Back to Prompts" />;
  if (!prompt)
    return (
      <p className="text-center text-default-secondary py-20">
        {t_i18n("Prompt not found")}
      </p>
    );

  const isLong = (prompt.content?.length ?? 0) > 600;

  return (
    <DetailShell
      backTo="/catalog/prompts"
      backLabel="Back to Prompts"
      entityType="prompts"
      entityId={prompt.id}
      icon={<FileText className="h-6 w-6 text-accent" />}
      name={prompt.name}
      description={prompt.description}
      tags={prompt.tags}
      createdAt={prompt.created_at}
      updatedAt={prompt.updated_at}
      sharedBy={prompt.shared_by}
      extraActions={
        prompt.content ? (
          <CopyButton text={prompt.content} label="Copy Content" />
        ) : undefined
      }
    >
      {prompt.content && (
        <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-2">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
          <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
            {t_i18n("Content")}
          </h2>
          <div
            className={cn(
              "relative content-base leading-relaxed prose prose-invert prose-sm max-w-none",
              "prose-headings:text-default-primary prose-p:text-default-secondary/90",
              "prose-strong:text-default-primary prose-li:text-default-secondary/90",
              "prose-code:text-accent prose-code:bg-elevation-hover prose-code:rounded-sm prose-code:px-1",
              !expanded && isLong && "overflow-hidden",
            )}
            style={
              !expanded && isLong ? { maxHeight: COLLAPSED_HEIGHT } : undefined
            }
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {prompt.content}
            </ReactMarkdown>
            {!expanded && isLong && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent" />
            )}
          </div>
          {isLong && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-1 content-compact text-accent hover:text-accent/80 transition-colors"
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  expanded && "rotate-180",
                )}
              />
              {expanded ? t_i18n("Collapse") : t_i18n("Expand full content")}
            </button>
          )}
        </div>
      )}
    </DetailShell>
  );
}
