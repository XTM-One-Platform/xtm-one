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
 * Continuing an investigation past its spent budget.
 *
 * Shown in the block's footer when a finished run left sources its plan named
 * and its allowance never reached. Three things, in the order an analyst needs
 * them: what the truncated plan actually *means*, what is left, and what
 * continuing would cost.
 *
 * **The assessment is composed here, from numbers the server counted.** No
 * sentence arrives from the backend, and no model wrote one. Two reasons that
 * point the same way: reasons in this product render from codes plus params so
 * the reader sees them in their own language, and a hallucinated "not worth
 * continuing" would close an investigation nobody decided to close — the same
 * reasoning that made subject classification a regex rather than a prompt.
 *
 * The line that earns its place is `packs_untouched`. "3 of 8 sources" is a
 * tally an analyst cannot act on; "no source from Internal telemetry was
 * reached" changes what the findings they are holding are *worth*.
 *
 * **Spend is shown against the chain's cap, not the run's.** The per-run
 * budget is the checkpoint interval — how often we come back and ask — and the
 * cumulative cap is the limit. Rendering only the run's figure would invite an
 * analyst to treat each continuation as free.
 */
import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Database,
  Fingerprint,
  Globe,
  Network,
  Search,
  Wrench,
} from "lucide-react";

import { AiActionGate } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import {
  useContinuationOffer,
  useStartContinuation,
} from "@/hooks/api/useInvestigationApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ContinuationCoverage } from "@/types";
import { Spinner } from "@filigran/design-system";

/** Icon per source kind. A corpus, a tool and a web page are different kinds
 *  of evidence, and which kind went unqueried is the point of the list. */
const KIND_ICONS: Record<string, typeof Globe> = {
  http: Globe,
  web_search: Search,
  knowledge_base: Database,
  mcp_tool: Wrench,
  site_fingerprint: Fingerprint,
  passive_dns: Network,
  opencti: Database,
};

/** Why the offer is closed. Keyed on the server's code so the wording is
 *  translatable and cannot drift from the rule that produced it. */
const BLOCKED_MESSAGES: Record<string, string> = {
  chain_budget:
    "This investigation has spent its whole allowance ({spent} of {max} queries). Start a new investigation to go further.",
  chain_depth:
    "This investigation has been continued as many times as allowed. Start a new one to go further.",
  nothing_left: "Every source this plan named was queried.",
  nothing_addressable:
    "What is left cannot be queried again: see the sources listed below.",
  in_flight: "A continuation of this investigation is still running.",
};
/** A reason this client does not know yet: said, never an empty line. */
const BLOCKED_FALLBACK = "This investigation cannot be continued.";

/** Why a source cannot be offered. */
const WITHHELD_MESSAGES: Record<string, string> = {
  no_address:
    "recorded before continuations existed, so it cannot be re-queried",
  unreadable_address: "its stored address cannot be read",
  no_longer_permitted: "you no longer have access to it",
};

/**
 * The derived assessment, as a list of sentences.
 *
 * Ordered by how much each changes a decision, not by how alarming it sounds.
 * The coverage gap comes first because it is the only one that can invalidate
 * the findings already in hand; the tally comes last because it is the one an
 * analyst can already see in the step list above.
 */
function useAssessment(
  coverage: ContinuationCoverage,
  packLabels: (slug: string) => string,
) {
  const { t_i18n } = useFormatter();
  return useMemo(() => {
    const lines: { tone: "warn" | "neutral"; text: string }[] = [];

    if (coverage.packs_untouched.length > 0) {
      lines.push({
        tone: "warn",
        text: t_i18n(
          "No source from {packs} was reached, so what you have rests on the rest of the plan alone.",
          { packs: coverage.packs_untouched.map(packLabels).join(", ") },
        ),
      });
    }
    if (coverage.queried > 0 && !coverage.findings) {
      lines.push({
        tone: "warn",
        text: t_i18n(
          "Every source that answered found nothing: the remaining sources are where an answer could still come from.",
        ),
      });
    } else if (coverage.empty > 0) {
      lines.push({
        tone: "neutral",
        text: t_i18n(
          "{empty} of {queried} sources answered with nothing found.",
          {
            empty: coverage.empty,
            queried: coverage.queried,
          },
        ),
      });
    }
    if (coverage.failed > 0) {
      lines.push({
        tone: "warn",
        text: t_i18n(
          "{failed, plural, one {# source} other {# sources}} failed or answered partially, so this plan covered less than the count suggests.",
          { failed: coverage.failed },
        ),
      });
    }
    lines.push({
      tone: "neutral",
      text: t_i18n("{queried} queried, {unqueried} never reached.", {
        queried: coverage.queried,
        unqueried: coverage.unqueried,
      }),
    });
    return lines;
  }, [coverage, packLabels, t_i18n]);
}

export function ContinuationPanel({
  investigationId,
  conversationId,
  enabled,
}: {
  investigationId: string;
  conversationId?: string | null;
  /** False while the run is unfinished. A run still in flight may yet query
   *  the very steps this would invite a second spend on. */
  enabled: boolean;
}) {
  const { t_i18n, t_text } = useFormatter();
  // Continuing queries sources and writes a report, so it is an AI action and
  // must be *visibly* unavailable when AI is off — not merely refused by the
  // backend gate after a click (FR-018). Both halves: the control carries the
  // disabled state, and `AiActionGate` supplies the shared explanation.
  const aiDisabled = useAiActionDisabled();
  const { data: offer, isLoading } = useContinuationOffer(
    investigationId,
    enabled,
  );
  const startContinuation = useStartContinuation(conversationId);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [rejected, setRejected] = useState<string[]>([]);

  // Labels come from the offer, never from a table here. The first version
  // kept one and it drifted immediately: `web-research` rendered as its raw
  // slug beside a properly labelled pack, in the very first screenshot of
  // this panel. The server resolves the packs, so it is the only place that
  // can name them without going stale.
  //
  // The slug remains the fallback rather than a blank: it IS what the run
  // recorded, so an unlabelled pack still reads correctly.
  // Run through `t_text`: a built-in's label is an English UI string, in the
  // catalogues — added there, which is the normal flow for a new pack, and
  // the reason a hardcoded map here was the wrong place for them. A built
  // pack's label is its author's name for it, shown as written.
  const packLabel = (slug: string) =>
    t_text(offer?.pack_labels?.[slug] ?? slug);

  const assessment = useAssessment(
    offer?.coverage ?? {
      queried: 0,
      unqueried: 0,
      findings: 0,
      empty: 0,
      failed: 0,
      packs_reached: [],
      packs_untouched: [],
    },
    packLabel,
  );

  if (!enabled || isLoading || !offer) return null;

  // Nothing was left and nothing was withheld: the run simply finished its
  // plan. Rendering an empty panel there would turn "complete" into a
  // question, so the block stays as it was.
  if (offer.blocked_reason === "nothing_left" && offer.withheld.length === 0) {
    return null;
  }

  const cap = offer.chain_max;
  const wouldSpend = Math.min(selected.size, offer.remaining_allowance);
  const truncated = selected.size > offer.remaining_allowance;

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="border-t border-elevation-subtle-soft bg-elevation-highlight">
      <div className="flex items-start gap-2 px-3 pt-2.5">
        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-feedback-warning-primary" />
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-medium text-default-primary">
            {offer.available
              ? t_i18n("{count, plural, one {# source was planned and never queried} other {# sources were planned and never queried}}", {
                  count: offer.steps.length,
                })
              : t_i18n("This plan was not finished")}
          </p>
          {/* The assessment. Derived, so it can be checked against the step
              list above rather than believed. */}
          <ul className="mt-1 space-y-0.5">
            {assessment.map((line) => (
              <li
                key={line.text}
                className={cn(
                  "content-caption leading-relaxed",
                  line.tone === "warn"
                    ? "text-feedback-warning-primary"
                    : "text-default-secondary",
                )}
              >
                {line.text}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {offer.available && (
        <>
          <ul className="mt-2 divide-y divide-[var(--border-elevation-subtle)] border-y border-elevation-subtle-soft">
            {offer.steps.map((step) => {
              const Icon = KIND_ICONS[step.kind] ?? Globe;
              const isOn = selected.has(step.step_id);
              return (
                <li key={step.step_id}>
                  <label
                    className={cn(
                      "flex cursor-pointer items-center gap-2 px-3 py-1.5 transition-colors",
                      isOn ? "bg-elevation-highlight/10" : "hover:bg-elevation-highlight",
                    )}
                  >
                    <Switch
                      className="shrink-0 scale-75"
                      checked={isOn}
                      onCheckedChange={() => toggle(step.step_id)}
                      aria-label={step.source_name}
                    />
                    <Icon className="h-3 w-3 shrink-0 text-default-secondary" />
                    <span className="min-w-0 flex-1 truncate content-caption text-default-primary">
                      {step.source_name}
                    </span>
                    {step.source_pack_slug && (
                      <span className="shrink-0 text-[9px] text-default-secondary">
                        {packLabel(step.source_pack_slug)}
                      </span>
                    )}
                  </label>
                </li>
              );
            })}
          </ul>

          <div className="flex items-center justify-between gap-2 px-3 py-2">
            <div className="min-w-0 content-caption text-default-secondary">
              {/* The cost reminder, against the CHAIN's cap. A per-run figure
                  would read as though each continuation started from zero. */}
              <span>
                {t_i18n("{spent} of {max} queries used", {
                  spent: offer.spent,
                  max: cap,
                })}
              </span>
              {offer.chain_depth > 1 && (
                <span>
                  {" · "}
                  {t_i18n("continued {count, plural, one {# time} other {# times}}", {
                    count: offer.chain_depth - 1,
                  })}
                </span>
              )}
              {truncated && (
                <span className="ml-1 text-feedback-warning-primary">
                  {t_i18n("· only {n} of your selection fits the allowance", {
                    n: wouldSpend,
                  })}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 content-caption"
                disabled={selected.size === 0 || startContinuation.isPending}
                onClick={() => setSelected(new Set())}
              >
                {t_i18n("Clear")}
              </Button>
              <AiActionGate>
                <Button
                  size="sm"
                  className="h-6 px-2 content-caption"
                  disabled={
                    aiDisabled ||
                    selected.size === 0 ||
                    startContinuation.isPending
                  }
                  onClick={async () => {
                    let result;
                    try {
                      result = await startContinuation.mutateAsync({
                        id: investigationId,
                        stepIds: [...selected],
                      });
                    } catch {
                      // Said by the global mutation toast, and the hook
                      // re-reads the offer; the selection is kept.
                      return;
                    }
                    // Reported, not swallowed: a step can stop being offerable
                    // between rendering this and clicking it, and the analyst
                    // needs to know which of their picks did not run.
                    setRejected(result.rejected_step_ids);
                    setSelected(new Set());
                  }}
                >
                  {startContinuation.isPending ? (
                    <>
                      <Spinner size="sm" tone="inherit" />
                      {t_i18n("Querying…")}
                    </>
                  ) : (
                    t_i18n("Query {count} selected", { count: wouldSpend })
                  )}
                </Button>
              </AiActionGate>
            </div>
          </div>
        </>
      )}

      {!offer.available && offer.blocked_reason && (
        <div className="flex items-start gap-1.5 px-3 pb-2 pt-1">
          <Ban className="mt-0.5 h-3 w-3 shrink-0 text-default-secondary" />
          <p className="content-caption text-default-secondary">
            {t_i18n(BLOCKED_MESSAGES[offer.blocked_reason] ?? BLOCKED_FALLBACK, {
              spent: offer.spent,
              max: cap,
            })}
          </p>
        </div>
      )}

      {/* Withheld sources are NAMED. A remainder that silently shrinks is the
          coverage gap this whole feature exists to close, so a source we
          cannot offer is listed with the reason rather than omitted. */}
      {offer.withheld.length > 0 && (
        <div className="border-t border-elevation-subtle-soft px-3 py-2">
          <p className="content-caption text-default-secondary">
            {t_i18n("Not offered:")}
          </p>
          <ul className="mt-0.5 space-y-0.5">
            {offer.withheld.map(([name, reason]) => (
              <li
                key={`${name}-${reason}`}
                className="content-caption text-default-secondary"
              >
                <Tip content={t_i18n(WITHHELD_MESSAGES[reason] ?? reason)}>
                  <span className="underline decoration-dotted underline-offset-2">
                    {name}
                  </span>
                </Tip>
                {" — "}
                {t_i18n(WITHHELD_MESSAGES[reason] ?? reason)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {rejected.length > 0 && (
        <div className="border-t border-elevation-subtle-soft px-3 py-2 content-caption text-feedback-warning-primary">
          {t_i18n(
            "{count} of your selection could not be queried: it stopped being available before the request ran.",
            { count: rejected.length },
          )}
        </div>
      )}
    </div>
  );
}
