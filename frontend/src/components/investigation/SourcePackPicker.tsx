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
 * Source-pack confirmation before a Deep Investigation starts.
 *
 * Steps 5 and 6 of the issue's activation flow: the subject has been
 * submitted, the server has classified it and ranked the packs, and the user
 * confirms or adjusts the selection before any source is queried.
 *
 * The design rule is the same one that governs the block: **never offer
 * coverage the run will not have.** A pack's affinity can cover a subject type
 * while none of its *sources* apply to it — `web-general` declares every
 * subject type but has no source for a file hash — so a pack contributing zero
 * applicable sources is shown with that count, disabled, and never
 * pre-selected. When no pack contributes anything the dialog says so and
 * refuses to start, rather than launching a run that would produce an empty
 * plan and a puzzled analyst.
 *
 * The server owns the ranking and the classification; this renders the order
 * it is given rather than re-sorting, so "which sources will run" has one
 * answer and it is the one that actually picks them.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, Globe, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SharedBadge } from "@/components/ui/shared-badge";
import { useSubjectPlan } from "@/hooks/api/useInvestigationApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { SourcePackOptions } from "@/types";

import { choiceHint, confirmedPackOptions, defaultPackOptions } from "./pack-options";
import { Spinner } from "@filigran/design-system";

export function SourcePackPicker({
  subject,
  preferredSlugs,
  onConfirm,
  onCancel,
}: {
  /** Subject awaiting confirmation; null closes the dialog. */
  subject: string | null;
  /** Packs to tick instead of the recommendation (a pack builder's "Run it
   *  in a chat"), among those that can contribute and are not a colleague's. */
  preferredSlugs?: string[] | null;
  /** The confirmed packs, and the options chosen under them (`pack-options`). */
  onConfirm: (slugs: string[], options: SourcePackOptions) => void;
  onCancel: () => void;
}) {
  const { t_i18n, t_text } = useFormatter();
  const { data: plan, isLoading, isError } = useSubjectPlan(subject);
  const [selected, setSelected] = useState<string[] | null>(null);
  // The choices made under each pack (a pivot's depth), seeded with the
  // defaults the server sends, once per subject like the selection.
  const [choices, setChoices] = useState<SourcePackOptions>({});

  // Packs that can actually contribute. A pack with no applicable source is
  // never selectable, so it can never be part of a confirmed selection.
  const selectable = useMemo(
    () => (plan?.packs ?? []).filter((p) => p.applicable_source_count > 0),
    [plan],
  );

  // Seed from the server's recommendation ONCE PER SUBJECT, and only among
  // packs that can contribute — a "recommended" pack with zero applicable
  // sources must not arrive pre-checked.
  //
  // Keyed on the subject rather than on the `plan` object identity: any
  // refetch while the dialog is open (window refocus, cache eviction, the
  // staleTime boundary) produces a new object, and re-running the seed then
  // discarded whatever the user had unchecked and snapped the selection back
  // to the recommendation — silently, right before they confirmed it.
  const seededForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!plan) {
      seededForRef.current = null;
      setSelected(null);
      return;
    }
    if (seededForRef.current === plan.subject) return;
    seededForRef.current = plan.subject;
    // Only the recommendation is preselected. With none, nothing is: a pack
    // that is not recommended (a knowledge base, a designated MCP tool) is
    // queried only when the user picks it. A colleague's pack is never
    // ticked for the user, not even by a `?pack=` link anyone can send.
    const preferred = selectable.filter((p) => preferredSlugs?.includes(p.slug) && p.origin !== "shared");
    setSelected((preferred.length ? preferred : selectable.filter((p) => p.recommended)).map((p) => p.slug));
    setChoices(defaultPackOptions(selectable));
  }, [plan, selectable, preferredSlugs]);

  const choose = (slug: string, key: string, value: string) =>
    setChoices((prev) => ({ ...prev, [slug]: { ...prev[slug], [key]: value } }));

  const chosen = selected ?? [];
  const totalSources = selectable
    .filter((p) => chosen.includes(p.slug))
    .reduce((n, p) => n + p.applicable_source_count, 0);

  const toggle = (slug: string) =>
    setSelected((prev) => {
      const current = prev ?? [];
      return current.includes(slug)
        ? current.filter((s) => s !== slug)
        : [...current, slug];
    });

  return (
    <Dialog open={!!subject} onOpenChange={(open) => !open && onCancel()}>
      {/* `min-w-0` on every direct child, and it is load-bearing rather than
          defensive. `DialogContent` is a `grid`, and a grid child defaults to
          `min-width: auto` — so a `white-space: nowrap` descendant (the
          source-name preview below carried `truncate`) sets the MIN-CONTENT
          width of the whole column. The column then exceeds the dialog's
          `max-w`, the content spills past its right edge, and `truncate`
          never truncates because its box is as wide as its text. Measured
          before and after on the shipped classes: 909px of content in a 560px
          box, versus no overflow once the children may shrink.

          `max-w-2xl` because the content genuinely wants the room: a pack row
          carries a description and a preview of up to a dozen corpus names,
          and 32rem made every one of them a clipped line.

          Written out on each child rather than as `[&>*]:min-w-0`: the whole
          fix depends on that rule being emitted, and a core utility is
          certainly emitted where an arbitrary variant depends on the scanner
          seeing it. Verbose beats clever when the failure mode is silent. */}
      <DialogContent className="max-w-2xl">
        <DialogHeader className="min-w-0">
          <DialogTitle className="flex items-center gap-2">
            <Search className="h-4 w-4 text-accent" />
            {t_i18n("Choose the sources to investigate")}
          </DialogTitle>
          <DialogDescription>
            {plan
              ? t_i18n("{subject} was identified as {type}.", {
                  // A pasted list reads as its summary, not 56 lines.
                  subject: plan.subject_summary || plan.subject,
                  type: plan.subject_type,
                })
              : t_i18n("Identifying the subject…")}
          </DialogDescription>
        </DialogHeader>

        {isLoading && (
          <div className="flex min-w-0 items-center gap-2 py-6 content-compact text-default-secondary">
            <Spinner size="sm" tone="inherit" className="text-accent" />
            {t_i18n("Identifying the subject…")}
          </div>
        )}

        {isError && (
          <div className="min-w-0 py-6 content-compact text-default-secondary">
            {t_i18n("The available sources could not be loaded.")}
          </div>
        )}

        {plan && !plan.has_applicable_source && (
          <div className="flex min-w-0 items-start gap-2 rounded-sm border border-feedback-alert-primary/40 bg-feedback-alert-primary/5 px-3 py-2.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
            <span className="content-compact text-default-secondary">
              {t_i18n(
                "No available source covers a {type} subject, so there is nothing to investigate yet. Covering it needs a source pack backed by an integration.",
                { type: plan.subject_type },
              )}
            </span>
          </div>
        )}

        {plan && plan.has_applicable_source && (
          <ul className="min-w-0 space-y-2">
            {plan.packs.map((pack) => {
              const usable = pack.applicable_source_count > 0;
              const isSelected = usable && chosen.includes(pack.slug);
              const destinations = pack.destinations ?? [];
              return (
                <li key={pack.slug}>
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={isSelected}
                    disabled={!usable}
                    onClick={() => usable && toggle(pack.slug)}
                    className={cn(
                      "w-full min-w-0 rounded-sm border px-3 py-2.5 text-left transition-colors",
                      !usable && "cursor-not-allowed opacity-50",
                      isSelected
                        ? "border-accent/50 bg-accent/5"
                        : "border-elevation-subtle hover:bg-elevation-highlight",
                    )}
                  >
                    <div className="flex items-start gap-2">
                      <div
                        className={cn(
                          "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border",
                          isSelected
                            ? "border-accent bg-accent/20"
                            : "border-elevation-subtle",
                        )}
                      >
                        {isSelected && <Check className="h-2.5 w-2.5 text-accent" />}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="content-compact font-medium text-default-primary">
                            {/* Through `t_text`, like the continuation panel
                                with the same string: a built-in's name is
                                translated (left raw, a French UI showed
                                English pack names), a built pack's is its
                                author's and shown as written. */}
                            {t_text(pack.label)}
                          </span>
                          {/* Whose the pack is comes from its row, so its
                              author cannot dress it up as a familiar one; the
                              name is the owner's own, so shown whole. */}
                          {pack.origin === "shared" && <SharedBadge ownerName={pack.owner_name} full />}
                          {pack.origin === "company" && <CompanyManagedBadge />}
                          {pack.recommended && (
                            <Badge variant="cyan" className="px-1.5 py-0 text-[9px]">
                              {t_i18n("recommended")}
                            </Badge>
                          )}
                          {/* The honest part: a pack that cannot contribute
                              says so instead of looking merely unselected. */}
                          <span
                            className={cn(
                              "content-caption",
                              usable
                                ? "text-default-secondary"
                                : "text-feedback-warning-primary",
                            )}
                          >
                            {usable
                              ? t_i18n("{n} of {total} sources apply", {
                                  n: pack.applicable_source_count,
                                  total: pack.total_source_count,
                                })
                              : t_i18n("no source applies to this subject")}
                          </span>
                        </div>
                        <div className="mt-0.5 content-caption break-words text-default-secondary">
                          {pack.description}
                        </div>
                        {usable && (
                          <div className="mt-0.5 line-clamp-2 content-caption break-words text-default-secondary/80">
                            {pack.applicable_source_names.join(" · ")}
                          </div>
                        )}
                        {/* Where the subject goes, from the URLs themselves:
                            a source's name says what its author wants. */}
                        {usable && destinations.length > 0 && (
                          <div
                            className={cn(
                              "mt-0.5 flex items-start gap-1 content-caption break-all",
                              pack.origin === "shared" ? "text-feedback-warning-primary" : "text-default-secondary",
                            )}
                          >
                            <Globe className="mt-px h-2.5 w-2.5 shrink-0" />
                            <span>{t_i18n("Sends the subject to {hosts}", { hosts: destinations.join(", ") })}</span>
                          </div>
                        )}
                      </div>
                    </div>
                  </button>
                  {/* A pack's options, under it once ticked: the server
                      describes them, so the picker names no pack. Outside
                      the row's button, which a radio cannot sit inside. */}
                  {isSelected &&
                    pack.options?.map((option) => {
                      const current = choices[pack.slug]?.[option.key] ?? option.default;
                      // An empty text is not a catalog key and makes t_i18n throw: not rendered.
                      const hint = choiceHint(option, current);
                      return (
                        <div
                          key={option.key}
                          className="mt-1.5 ml-6 min-w-0 rounded-sm border border-elevation-subtle-soft px-3 py-2"
                        >
                          <div className="content-caption break-words">
                            <span className="font-medium text-default-primary">{t_text(option.label)}</span>{" "}
                            {option.description && (
                              <span className="text-default-secondary">{t_text(option.description)}</span>
                            )}
                          </div>
                          <div
                            role="radiogroup"
                            aria-label={t_text(option.label)}
                            className="mt-1.5 flex flex-wrap gap-1.5"
                          >
                            {option.choices.map((choice) => (
                              <button
                                key={choice.value}
                                type="button"
                                role="radio"
                                aria-checked={choice.value === current}
                                onClick={() => choose(pack.slug, option.key, choice.value)}
                                className={cn(
                                  "rounded-sm border px-2 py-0.5 content-caption transition-colors",
                                  choice.value === current
                                    ? "border-accent/60 bg-accent/10 text-accent"
                                    : "border-elevation-subtle text-default-secondary hover:bg-elevation-highlight",
                                )}
                              >
                                {t_i18n(choice.label)}
                              </button>
                            ))}
                          </div>
                          {hint && (
                            <div className="mt-1 content-caption break-words text-default-secondary">
                              {t_i18n(hint)}
                            </div>
                          )}
                        </div>
                      );
                    })}
                </li>
              );
            })}
          </ul>
        )}

        {/* Plain footer row: this repo's dialog primitive exports no
            `DialogFooter`, and adding one to a shared component for a single
            caller would be the wrong place to grow it. */}
        <div className="flex min-w-0 flex-col items-stretch gap-2 pt-2 sm:flex-row sm:items-center sm:justify-between">
          <span className="content-caption text-default-secondary">
            {totalSources === 1
              ? t_i18n("1 source will be queried")
              : t_i18n("{n} sources will be queried", { n: totalSources })}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onCancel}>
              {t_i18n("Cancel")}
            </Button>
            <Button
              size="sm"
              disabled={totalSources === 0}
              onClick={() => onConfirm(chosen, confirmedPackOptions(selectable, chosen, choices))}
            >
              {t_i18n("Start investigation")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
