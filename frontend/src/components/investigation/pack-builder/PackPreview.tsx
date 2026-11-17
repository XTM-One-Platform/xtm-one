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
 * The pack builder's preview: what the pack being edited would do on a
 * sample subject, for the person editing it. No source is queried; the server
 * resolves the pack with the caller's own integrations and corpora, so the
 * preview says which steps are served, which are not and why.
 */
import { useState } from "react";
import { AlertTriangle, CheckCircle2, MessageSquare, Search } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { ACTION_ICONS } from "@/components/investigation/action-icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { usePreviewInvestigationPack } from "@/hooks/api/useInvestigationPackApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { InvestigationPackFields } from "@/types";

import { refusalText } from "./pack-draft";

export function PackPreview({
  draft,
  slug,
  dirty,
}: {
  draft: InvestigationPackFields;
  /** The saved pack's slug: "Run in chat" needs a pack the picker offers. */
  slug: string | null;
  /** Unsaved changes: a run uses the saved version. */
  dirty: boolean;
}) {
  const { t_i18n, t_text } = useFormatter();
  const navigate = useNavigate();
  const [subject, setSubject] = useState("");
  const preview = usePreviewInvestigationPack();
  const result = preview.data;

  const run = () => {
    const params = new URLSearchParams({ investigate: subject.trim() });
    if (slug) params.set("pack", slug);
    navigate(`/chat?${params.toString()}`);
  };

  return (
    <Card className="lg:sticky lg:top-4">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{t_i18n("Preview")}</CardTitle>
        <p className="content-compact text-default-secondary">
          {t_i18n("What the pack would do on a subject, with your own access. Nothing is queried.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (subject.trim()) preview.mutate({ subject: subject.trim(), pack: draft });
          }}
        >
          <Input
            aria-label={t_i18n("Sample subject")}
            value={subject}
            placeholder={t_i18n("e.g. armdailynews.com")}
            className="h-8 content-base"
            onChange={(e) => setSubject(e.target.value)}
          />
          <Button type="submit" size="sm" className="gap-1.5" disabled={!subject.trim() || preview.isPending}>
            <Search className="h-3.5 w-3.5" />
            {t_i18n("Preview")}
          </Button>
        </form>

        {preview.isError && (
          <p className="content-compact text-feedback-warning-primary">{refusalText(preview.error) || t_i18n("The preview could not be computed.")}</p>
        )}

        {result && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 content-compact">
              <span className="text-default-secondary">{t_i18n("Identified as")}</span>
              <Badge variant="outline" className="content-caption">{result.subject_type}</Badge>
              {result.reachable ? (
                <span className="flex items-center gap-1 text-feedback-success-primary">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {t_i18n("Every step is served")}
                </span>
              ) : (
                <span className="flex items-center gap-1 text-feedback-warning-primary">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {t_i18n("Some steps are not served for you")}
                </span>
              )}
            </div>
            <ol className="space-y-1.5" data-pack-preview>
              {result.actions.map((action, i) => {
                const Icon = ACTION_ICONS[action.icon] ?? ACTION_ICONS.target;
                return (
                  <li
                    key={action.slug}
                    className={cn(
                      "rounded-sm border px-2.5 py-1.5",
                      action.servable ? "border-elevation-subtle" : "border-feedback-alert-primary/40 bg-feedback-alert-primary/5",
                    )}
                  >
                    <div className="flex items-center gap-2 content-compact">
                      <Icon className="h-3.5 w-3.5 text-accent" />
                      <span className="tabular-nums text-default-secondary">{i + 1}.</span>
                      <span className="font-medium">{t_text(action.label)}</span>
                      {!action.servable && (
                        <span className="ml-auto content-caption text-feedback-warning-primary">{t_i18n("not served")}</span>
                      )}
                    </div>
                    {action.sources.length > 0 && (
                      <div className="mt-0.5 pl-6 content-caption text-default-secondary">
                        {action.sources.map((s) => s.name).join(" · ")}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
            {result.description && (
              <p className="text-[11px] text-default-secondary">{result.description}</p>
            )}
          </div>
        )}

        <div className="border-t border-elevation-subtle pt-3">
          <Button
            variant="outline"
            size="sm"
            className="w-full gap-1.5"
            disabled={!slug || !subject.trim()}
            onClick={run}
          >
            <MessageSquare className="h-3.5 w-3.5" />
            {t_i18n("Run it in a chat")}
          </Button>
          <p className="mt-1.5 content-caption text-default-secondary">
            {!slug
              ? t_i18n("Save the pack to run it.")
              : dirty
                ? t_i18n("A run uses the saved version; save your changes first.")
                : t_i18n("Opens a chat with Deep Investigation armed and this pack ticked; the run queries its sources.")}
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
