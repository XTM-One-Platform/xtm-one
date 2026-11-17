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
 * The pack builder's assistant panel: describe the method, the
 * assistant rewrites the draft. Each answer is a whole pack the server already
 * validated; it replaces the draft in the editor (with an undo), and nothing is
 * saved until the person saves. The conversation lives in the panel only.
 */
import { useEffect, useRef, useState } from "react";
import { Sparkles, Undo2 } from "lucide-react";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAssistInvestigationPack } from "@/hooks/api/useInvestigationPackApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { InvestigationPackFields } from "@/types";

import { type AssistTurn, assistHistory, refusalText } from "./pack-draft";
import { Spinner } from "@filigran/design-system";

/** Shown to whoever may edit the pack only. */
export function PackAssistant({
  draft,
  onApply,
}: {
  draft: InvestigationPackFields;
  onApply: (next: InvestigationPackFields) => void;
}) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const assist = useAssistInvestigationPack();
  const [turns, setTurns] = useState<AssistTurn[]>([]);
  const [instruction, setInstruction] = useState("");
  const [previous, setPrevious] = useState<InvestigationPackFields | null>(null);
  // The draft as it is when the answer lands, edits made meanwhile included:
  // what Undo puts back.
  const latest = useRef(draft);
  useEffect(() => {
    latest.current = draft;
  }, [draft]);

  const send = () => {
    const text = instruction.trim();
    if (!text || aiOff || assist.isPending) return;
    const history = assistHistory(turns);
    setTurns((t) => [...t, { role: "user", content: text }]);
    setInstruction("");
    assist.mutate(
      { instruction: text, current: draft, history },
      {
        onSuccess: ({ reply, pack }) => {
          setTurns((t) => [...t, { role: "assistant", content: reply }]);
          setPrevious(latest.current);
          onApply(pack);
        },
        onError: (error) =>
          setTurns((t) => [
            ...t,
            { role: "assistant", failed: true, content: refusalText(error) || t_i18n("The assistant could not build a pack.") },
          ]),
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Sparkles className="h-4 w-4 text-accent" />
          {t_i18n("Assistant")}
        </CardTitle>
        <p className="content-compact text-default-secondary">
          {t_i18n("Describe the method; the assistant rewrites the draft. Review it, then save.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {turns.length > 0 && (
          <ol className="max-h-64 space-y-2 overflow-y-auto content-compact" data-pack-assistant>
            {turns.map((turn, i) => (
              <li
                key={i}
                className={cn(
                  "rounded-sm px-2.5 py-1.5",
                  turn.role === "user" ? "ml-6 bg-elevation-hover" : "mr-6 border border-accent/30 bg-accent/5",
                )}
              >
                {turn.content}
              </li>
            ))}
            {assist.isPending && (
              <li className="flex items-center gap-2 text-default-secondary">
                <Spinner size="sm" tone="inherit" />
                {t_i18n("Building the pack…")}
              </li>
            )}
          </ol>
        )}
        <textarea
          aria-label={t_i18n("What the pack should do")}
          value={instruction}
          rows={3}
          maxLength={4000}
          className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-compact"
          placeholder={t_i18n("e.g. Map a news site's hosting, pivot on its IP, and check every neighbour in OpenCTI")}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
          }}
        />
        <div className="flex items-center gap-2">
          {previous && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5"
              onClick={() => {
                onApply(previous);
                setPrevious(null);
              }}
            >
              <Undo2 className="h-3.5 w-3.5" />
              {t_i18n("Undo the change")}
            </Button>
          )}
          <AiDisabledTip>
            <Button
              size="sm"
              className="ml-auto gap-1.5"
              disabled={aiOff || assist.isPending || !instruction.trim()}
              onClick={send}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {t_i18n("Ask the assistant")}
            </Button>
          </AiDisabledTip>
        </div>
      </CardContent>
    </Card>
  );
}
