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
 * Prose with its citations rendered where they belong — inline.
 *
 * A provider web search does not return links, it returns an ANSWER, and it
 * says which span of that answer each source supports. Rendering that as a
 * list of sources underneath — which is what this platform did everywhere
 * before — throws the useful half away: a reader sees that five pages were
 * consulted and not which sentence any of them backs.
 *
 * The markers are woven into the markdown as links and the whole thing is
 * handed to the chat renderer, so an answer that arrives with headings, a
 * table and a list still renders as one. The numbered list underneath is a
 * key, not the citation.
 *
 * A citation with no span is never placed. It is real — the source was
 * consulted — but nothing says where, and a marker dropped at a plausible spot
 * attributes a sentence to a page that may not support it.
 */
import { useMemo } from "react";

import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Tip } from "@/components/ui/tooltip";
import {
  numberCitations,
  safeHref,
  weaveCitations,
} from "@/components/investigation/citations";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { InvestigationEvidence } from "@/types";

export function CitedAnswer({
  answer,
  evidence,
  className,
}: {
  answer: string;
  evidence: InvestigationEvidence[];
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { anchored, ordered } = useMemo(() => numberCitations(evidence), [evidence]);
  const woven = useMemo(() => weaveCitations(answer, anchored), [answer, anchored]);

  if (!answer.trim()) return null;

  return (
    <div className={cn("space-y-2", className)}>
      <ChatMarkdown content={woven} className="chat-markdown content-compact" />

      {/* Said plainly rather than left to be noticed: prose whose sources are
          all unplaced looks identical to prose nobody sourced. Above the list
          it introduces. */}
      {anchored.length === 0 && ordered.length > 0 && (
        <p className="content-caption text-feedback-warning-primary">
          {t_i18n("The sources below were consulted, but not tied to a specific sentence.")}
        </p>
      )}

      {ordered.length > 0 && (
        <ol className="space-y-0.5 border-t border-elevation-subtle-soft pt-1.5">
          {ordered.map((cite) => (
            <li
              key={`${cite.n}-${cite.href ?? cite.label}`}
              className="flex items-start gap-1.5 content-caption text-default-secondary"
            >
              <span className="shrink-0 text-accent">[{cite.n}]</span>
              <Tip content={cite.quote || undefined}>
                {safeHref(cite.href) ? (
                  <a
                    href={safeHref(cite.href)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="truncate underline decoration-dotted underline-offset-2 hover:text-default-primary"
                  >
                    {cite.label}
                  </a>
                ) : (
                  <span className="truncate">{cite.label}</span>
                )}
              </Tip>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
