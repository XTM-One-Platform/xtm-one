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

import { useState, useRef, useEffect } from "react";
import { Search, X } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTestKBSearch } from "@/hooks/api/useKnowledgeBaseApi";
import { formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { KnowledgeBase, RAGSearchResponse } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Spinner } from "@filigran/design-system";

export function TestSearchPanel({ kb, onClose }: { kb: KnowledgeBase; onClose: () => void }) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const testSearch = useTestKBSearch();
  const [results, setResults] = useState<RAGSearchResponse | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const handleSearch = () => {
    if (!query.trim()) return;
    testSearch.mutate(
      { kbId: kb.id, query: query.trim(), top_k: 5 },
      { onSuccess: (data) => setResults(data) },
    );
  };

  return (
    <Card className="border-accent/20">
      <CardContent className="pt-5 pb-4 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-accent" />
            <h3 className="content-base font-semibold">{t_i18n("Test RAG Search")}</h3>
          </div>
          <Button aria-label={t_i18n("Close")} variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={onClose}>
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>

        <div className="flex gap-2">
          <Input aria-label={t_i18n("Enter a search query to test against this knowledge base...")}
            ref={inputRef}
            placeholder={t_i18n("Enter a search query to test against this knowledge base...")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            className="flex-1"
          />
          <AiDisabledTip><Button className="h-10 gap-1.5 shrink-0" onClick={handleSearch} disabled={testSearch.isPending || !query.trim() || aiOff} aria-busy={testSearch.isPending || undefined}>
            {testSearch.isPending && <Spinner size="sm" tone="inherit" />}{!testSearch.isPending && <Search className="h-3.5 w-3.5" />}
            {t_i18n("Search")}
          </Button></AiDisabledTip>
        </div>

        {testSearch.isError && (
          <div className="rounded-sm bg-feedback-error-primary/5 p-3">
            <p className="content-compact text-feedback-error-primary">{testSearch.error.message}</p>
          </div>
        )}

        {results && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="content-compact text-default-secondary">
                {results.total === 0
                  ? t_i18n("No results found")
                  : t_i18n("{count, plural, one {# result found} other {# results found}}", { count: results.total })}
              </p>
              {results.total > 0 && (
                <p className="content-caption text-default-secondary">{t_i18n("Sorted by relevance")}</p>
              )}
            </div>

            {results.results.map((result, i) => {
              const pct = Math.round(result.score * 100);
              return (
                <div key={i} className="rounded-sm bg-elevation-hover p-3 space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-[9px] font-mono-plex">{result.source_type}</Badge>
                      {result.source_id && (
                        <span className="text-[9px] text-default-secondary truncate max-w-[200px]">{result.source_id}</span>
                      )}
                    </div>
                    <Badge
                      variant={pct >= 80 ? "success" : pct >= 50 ? "cyan" : "outline"}
                      className="text-[9px] shrink-0 tabular-nums"
                    >
                      {t_i18n("{pct}% match", { pct })}
                    </Badge>
                  </div>
                  <p className="content-compact text-default-secondary leading-relaxed whitespace-pre-wrap line-clamp-6">
                    {result.content}
                  </p>
                </div>
              );
            })}
          </div>
        )}

        {!results && !testSearch.isPending && (
          <p className="text-[11px] text-default-secondary text-center py-2">
            {t_i18n("Enter a query above to test semantic search against the {count} embedded chunks.", { count: formatCompactNumber(kb.chunk_count) })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
