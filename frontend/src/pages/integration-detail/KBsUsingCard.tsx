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

import { useNavigate } from "react-router-dom";
import { Database, ArrowRight } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CountBadge } from "@/components/ui/count-badge";
import { useKBsByIntegration } from "@/hooks/api/useKnowledgeBaseApi";
import { formatKBStats } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

export function KBsUsingCard({ integrationId }: { integrationId: string }) {
  const { t_i18n } = useFormatter();
  const { data: kbs, isLoading } = useKBsByIntegration(integrationId, { scope: "all" });
  const navigate = useNavigate();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Database className="h-4 w-4 text-accent" />
          {t_i18n("Knowledge Bases Using This Integration")}
          {kbs && <CountBadge variant="cyan" className="ml-auto">{kbs.length}</CountBadge>}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex justify-center py-6"><Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} /></div>
        ) : !kbs || kbs.length === 0 ? (
          <div className="text-center py-6">
            <Database className="h-8 w-8 text-default-disabled mx-auto mb-2" />
            <p className="content-base text-default-secondary">{t_i18n("No knowledge bases are syncing from this integration.")}</p>
            <p className="content-compact text-default-secondary mt-1">{t_i18n("Add this integration as an import source in a knowledge base.")}</p>
          </div>
        ) : (
          <div className="space-y-2">
            {kbs.map((kb) => (
              <button
                key={kb.id}
                onClick={() => navigate(`/knowledge/${kb.id}`)}
                className="w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 text-left transition-colors hover:bg-elevation-highlight group"
              >
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                  <Database className="h-4 w-4 text-accent" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="content-base font-medium truncate">{kb.name}</p>
                  <p className="content-caption text-default-secondary">{formatKBStats(kb.document_count, kb.chunk_count)}</p>
                </div>
                <ArrowRight className="h-3.5 w-3.5 text-default-disabled group-hover:text-accent transition-colors shrink-0" />
              </button>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
