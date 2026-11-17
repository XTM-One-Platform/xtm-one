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

import { Icon } from "@filigran/design-system";
/**
 * "How it works" on the fleet About page (#10327): the trigger chips, each
 * explained in a tooltip from the configuration, then one compact numbered row
 * per step, its title in bold and its AI-written markdown body muted under it.
 * Past STEPS_FOLD steps the list folds behind "Show all N steps" (a button,
 * never a scroll box) and each body is clamped to two lines until unfolded.
 * Same-named automations count in a chip's tooltip, so its numbers add up. A legacy step without a body reads its title only.
 */
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import type { FleetDocStructured } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { triggerKindLabel } from "./fleet-about-showcase";
import { STEPS_FOLD, type TriggerKindSummary, triggerKindSummaries, visibleSteps } from "./fleet-about-steps";

const SAFE_LINK = /^(https?:|mailto:|\/(?!\/)|#)/i;

/** What a step body may draw: inline marks, paragraphs, short lists and links. No HTML, image, heading or table. */
const STEP_ELEMENTS = ["p", "strong", "em", "code", "ul", "ol", "li", "a", "br", "del"];

const STEP_BODY_CLASS =
  "min-w-0 break-words content-compact text-default-secondary [&_a]:text-accent [&_a]:underline [&_code]:rounded-sm [&_code]:bg-elevation-hover [&_code]:px-1 [&_li]:ml-4 [&_ol]:list-decimal [&_p+p]:mt-1 [&_p]:my-0 [&_strong]:font-semibold [&_strong]:text-default-primary [&_ul]:mt-1 [&_ul]:list-disc";

/** The names a chip's tooltip lists, a name shared by several automations followed by how many ("Inbox (3)"). */
const chipNames = (c: TriggerKindSummary) => c.names.map((n) => (n.count > 1 ? `${n.name} (${n.count})` : n.name)).join(", ");

/**
 * A step body in markdown, sanitized: raw HTML is skipped, anything outside STEP_ELEMENTS keeps its text only.
 * Clamped to two lines while the list is folded, so the steps column stays within the mini graph's height.
 */
function StepBody({ text, clamped }: { text: string; clamped: boolean }) {
  return (
    <div
      className={cn(STEP_BODY_CLASS, clamped && "line-clamp-2")}
      data-testid="about-step-body"
      data-clamped={clamped || undefined}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        allowedElements={STEP_ELEMENTS}
        unwrapDisallowed
        components={{
          a: ({ href, children }) =>
            href && SAFE_LINK.test(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function FleetAboutSteps({
  structured,
  onExpandedChange,
}: {
  structured: FleetDocStructured | null | undefined;
  /** Told when the list unfolds or folds back: the page then lets the diagram card keep its own height. */
  onExpandedChange?: (expanded: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpandedState] = useState(false);
  const setExpanded = (next: boolean) => {
    setExpandedState(next);
    onExpandedChange?.(next);
  };
  const chips = triggerKindSummaries(structured);
  const all = visibleSteps(structured?.steps, true).shown;
  const { shown, hidden } = visibleSteps(structured?.steps, expanded);
  const chipTip = (c: (typeof chips)[number]) =>
    t_i18n("{count, plural, one {# automation starts this way: {names}} other {# automations start this way: {names}}}", {
      count: c.count,
      names: c.more > 0 ? `${chipNames(c)} ${t_i18n("{count, plural, one {and # more} other {and # more}}", { count: c.more })}` : chipNames(c),
    });

  return (
    <>
      {chips.length > 0 && (
        <ul className="flex flex-wrap items-center gap-1.5" data-testid="about-trigger-kinds">
          {chips.map((c) => (
            <li key={c.kind}>
              <Tip content={chipTip(c)} side="top">
                <BadgeFocusTrigger aria-label={`${triggerKindLabel(c.kind, t_i18n)}. ${chipTip(c)}`} data-testid="about-trigger-kind">
                  <Badge variant="secondary">{triggerKindLabel(c.kind, t_i18n)}</Badge>
                </BadgeFocusTrigger>
              </Tip>
            </li>
          ))}
        </ul>
      )}
      {all.length > 0 && (
        <ol className="space-y-3" data-testid="about-steps">
          {shown.map((st, i) => (
            <li key={i} className="flex min-w-0 items-start gap-3" data-testid="about-step">
              <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/10 content-compact font-semibold text-accent">{i + 1}</span>
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex min-h-6 min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 content-compact font-semibold">{st.title}</span>
                  {st.gate && (
                    <Badge variant="warning" className="shrink-0 gap-1">
                      <Icon name="shield-check" size={16} />
                      {t_i18n("A person approves first")}
                    </Badge>
                  )}
                </div>
                {st.detail && <StepBody text={st.detail} clamped={!expanded && all.length > STEPS_FOLD} />}
              </div>
            </li>
          ))}
        </ol>
      )}
      {(hidden > 0 || (expanded && all.length > STEPS_FOLD)) && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 self-start"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
          data-testid="about-steps-toggle"
        >
          {expanded ? t_i18n("Show fewer") : t_i18n("{count, plural, one {Show all # steps} other {Show all # steps}}", { count: all.length })}
        </Button>
      )}
    </>
  );
}
