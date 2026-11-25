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

// Diff: the pair's objects, git style, split (the reference left, the answer right) or
// unified (− the reference, + the answer), one hunk per object type then the relationships.

import { useId, type ReactNode } from "react";
import { Switch } from "@/components/ui/switch";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  displayStatus,
  entityVerdict,
  entityVisible,
  f1,
  formatScore,
  relationshipCounts,
  relationshipVerdict,
  relationshipVisible,
  tripleText,
  typeCounts,
  wordDiff,
  type DisplayStatus,
  type EntityItem,
  type RelationshipItem,
} from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { STATUS_SURFACE, statusLabel } from "./labels";
import { noteFor } from "./graph-text";
import { OkMark } from "./ReviewInspector";
import { isSelected, type ReviewContext } from "./context";

export type DiffLayout = "split" | "unified";

interface Side {
  text: string;
  sub?: string;
}

function sideOf(it: EntityItem | RelationshipItem, s: string): Side | null {
  if ("r" in it) {
    const t = tripleText(it.r, s);
    return t ? { text: t } : null;
  }
  const o = it.g.by[s];
  return o ? { text: o.name + (o.type !== it.g.type ? ` (${o.type})` : ""), sub: o.cid } : null;
}

/** What changed on this row from the comparator (the previous run) to the answer under review. */
function historyVerdict(ctx: ReviewContext, it: EntityItem | RelationshipItem): DisplayStatus | null {
  const prev = ctx.comparator?.key;
  if (!prev || ctx.mode === "runs" || ctx.review.unreadable.includes(prev)) return null;
  const B = ctx.pair.under;
  if ("r" in it) {
    const r = ctx.fullModel.relById.get(it.r.id);
    return r ? relationshipVerdict(ctx.fullModel, r, prev, B, ctx.whatIf) : null;
  }
  const g = ctx.fullModel.byId.get(it.g.id);
  return g ? entityVerdict(g, prev, B, ctx.whatIf) : null;
}

/** A side's text, with the part that differs from the other side marked. */
function Marked({ parts, tone }: { parts: [string, string, string] | null; tone: "del" | "add" }) {
  if (!parts) return null;
  return (
    <>
      {parts[0]}
      {parts[1] && (
        <mark className={cn("rounded-sm text-default-primary", tone === "del" ? "bg-feedback-error-primary/20" : "bg-feedback-success-primary/20")}>
          {parts[1]}
        </mark>
      )}
      {parts[2]}
    </>
  );
}

function DiffRow({ ctx, it, layout }: { ctx: ReviewContext; it: EntityItem | RelationshipItem; layout: DiffLayout }) {
  const { t_i18n } = useFormatter();
  const { ref: A, under: B } = ctx.pair;
  const isRel = "r" in it;
  const id = isRel ? it.r.id : it.g.id;
  const kind = isRel ? "r" : "e";
  const st = displayStatus(it.st, ctx.mode, ctx.whatIf);
  const a = ctx.mode === "alone" ? null : sideOf(it, A);
  const b = sideOf(it, B);
  const same = !!a && !!b && a.text === b.text && a.sub === b.sub;
  const diff = a && b && !same ? wordDiff(a.text, b.text) : null;
  const pa: [string, string, string] | null = a ? diff?.a ?? [a.text, "", ""] : null;
  const pb: [string, string, string] | null = b ? diff?.b ?? [b.text, "", ""] : null;
  const markA = a && A !== EXPECTED ? <OkMark ctx={ctx} row={isRel ? it.r : undefined} group={isRel ? undefined : it.g} source={A} /> : null;
  const markB = b && ctx.mode !== "score" ? <OkMark ctx={ctx} row={isRel ? it.r : undefined} group={isRel ? undefined : it.g} source={B} /> : null;
  const note = noteFor(ctx, it, t_i18n);
  const selected = isSelected(ctx, kind, id);
  const select = () => ctx.select({ kind, id });
  const label = t_i18n(statusLabel(st, ctx.mode));
  // Against the reference in a comparison of two runs; else, what changed since the
  // previous run of the same agent and model on this report.
  const verdict = ctx.mode === "runs" ? st : historyVerdict(ctx, it);
  const chip =
    verdict && verdict !== "same" ? (
      <span
        title={ctx.mode === "runs" ? undefined : t_i18n("vs {label}", { label: ctx.comparator?.name ?? "" })}
        className={cn("rounded-sm border px-2 content-caption text-default-primary", STATUS_SURFACE[verdict])}
      >
        {t_i18n(statusLabel(verdict, "runs"))}
      </span>
    ) : null;
  const gone = ctx.mode !== "alone" && !b;
  const rowClass = cn(
    "w-full rounded-sm border-l-2 px-2 py-1 text-left content-compact transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
    STATUS_SURFACE[st],
    selected && "ring-2 ring-accent",
  );
  if (layout === "split" && ctx.mode !== "alone") {
    return (
      <button type="button" aria-pressed={selected} aria-label={`${label}: ${(b ?? a)?.text ?? ""}`} onClick={select} className={rowClass}>
        <span className="grid grid-cols-[1fr_1.5rem_1fr_auto] items-start gap-2">
          <span className={cn("min-w-0 break-words", !a && "text-default-disabled")}>
            {markA}
            <Marked parts={pa} tone="del" />
            {a?.sub && <span className="block content-caption font-mono-plex text-default-secondary break-all">{a.sub}</span>}
          </span>
          <span aria-hidden className="text-center text-default-secondary">{same ? "=" : a && b ? "≈" : a ? "−" : "+"}</span>
          <span className={cn("min-w-0 break-words", gone && "text-default-disabled")}>
            {markB}
            <Marked parts={pb} tone="add" />
            {b?.sub && <span className="block content-caption font-mono-plex text-default-secondary break-all">{b.sub}</span>}
          </span>
          <span>{chip}</span>
        </span>
        {note && <span className="mt-0.5 block content-caption text-default-secondary">↳ {note}</span>}
      </button>
    );
  }
  const ty = isRel ? t_i18n("relationship") : it.g.type;
  const line = (sign: string, side: Side, parts: [string, string, string] | null, mark: ReactNode, tone: "del" | "add", withChip: boolean) => (
    <span className="grid grid-cols-[1rem_8rem_1fr_auto] items-start gap-2">
      <span aria-hidden className="text-default-secondary">{sign}</span>
      <span className="truncate font-mono-plex text-default-secondary">{ty}</span>
      <span className="min-w-0 break-words">
        {mark}
        <Marked parts={parts} tone={tone} />
        {side.sub && <span className="ml-2 content-caption font-mono-plex text-default-secondary break-all">{side.sub}</span>}
      </span>
      <span>{withChip ? chip : null}</span>
    </span>
  );
  return (
    <button type="button" aria-pressed={selected} aria-label={`${label}: ${(b ?? a)?.text ?? ""}`} onClick={select} className={rowClass}>
      {ctx.mode === "alone" || same ? (
        b && line(" ", b, pb, markB, "add", true)
      ) : (
        <>
          {a && line("−", a, pa, markA, "del", true)}
          {b && line("+", b, pb, markB, "add", !a)}
        </>
      )}
      {note && <span className="mt-0.5 block pl-6 content-caption text-default-secondary">↳ {note}</span>}
    </button>
  );
}

export function DiffView({
  ctx,
  layout,
  onLayout,
  hideMatched,
  onHideMatched,
}: {
  ctx: ReviewContext;
  layout: DiffLayout;
  onLayout: (layout: DiffLayout) => void;
  hideMatched: boolean;
  onHideMatched: (hide: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const switchId = useId();
  const { model, mode, pair, whatIf, derived, filters } = ctx;
  const { ref: A, under: B } = pair;
  const keep = (it: EntityItem | RelationshipItem) => {
    const st = displayStatus(it.st, mode, whatIf);
    return !(hideMatched && (st === "tp" || st === "same"));
  };
  const hunks = model.types
    .map((type) => {
      const rows = derived.entItems.filter((it) => it.g.type === type && entityVisible(it, filters, mode) && keep(it));
      if (!rows.length) return null;
      const under = f1(typeCounts(model, B, whatIf, type));
      const head =
        mode === "runs"
          ? t_i18n("F1 {from} → {to}", { from: formatScore(f1(typeCounts(model, A, whatIf, type))), to: formatScore(under) })
          : mode === "alone"
            ? t_i18n("{count} in {answer} · F1 {value}", {
                count: model.groups.filter((g) => g.type === type && g.by[B]).length,
                answer: ctx.letter(B),
                value: formatScore(under),
              })
            : t_i18n("expected {expected} · {answer} {count} · F1 {value}", {
              expected: model.groups.filter((g) => g.type === type && g.by[EXPECTED]).length,
              answer: ctx.letter(B),
              count: model.groups.filter((g) => g.type === type && g.by[B]).length,
              value: formatScore(under),
            });
      return { key: type, title: type, head, rows };
    })
    .filter((h): h is NonNullable<typeof h> => !!h);
  const relRows = derived.relItems.filter((it) => relationshipVisible(model, it, filters, mode) && keep(it));
  const help =
    mode === "runs"
      ? t_i18n("What changed between the two answers, each judged against the expected bundle: ✓ expected, ≈ expected in another form, ✗ not expected. Green rows gain points, red rows lose them.")
      : mode === "alone"
        ? t_i18n("Everything the answer extracted, checked against the expected bundle: ✓ expected, ≈ expected in another form, ✗ not expected.")
        : t_i18n("Objects are paired by canonical ID. A near-miss pairs two IDs that differ only by an alias, a wording, a type or a defanged value.");
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="content-compact text-default-secondary">{help}</p>
        <div className="flex items-center gap-3">
          <label htmlFor={switchId} className="flex items-center gap-2 content-compact">
            <Switch id={switchId} checked={hideMatched} onCheckedChange={onHideMatched} />
            {mode === "runs" ? t_i18n("Hide unchanged") : mode === "alone" ? t_i18n("Hide correct") : t_i18n("Hide matched")}
          </label>
          {mode !== "alone" && (
            <SegmentedFilter
              selection="radio"
              size="sm"
              ariaLabel={t_i18n("Diff layout")}
              value={layout}
              onChange={onLayout}
              options={[
                { key: "split", label: "Split" },
                { key: "unified", label: "Unified" },
              ]}
            />
          )}
        </div>
      </div>
      {layout === "split" && mode !== "alone" && (
        <div className="grid grid-cols-[1fr_1.5rem_1fr_auto] gap-2 px-2 content-caption font-medium uppercase tracking-wider text-default-secondary">
          <span>{A === EXPECTED ? t_i18n("Expected") : `${ctx.letter(A)} · ${ctx.sourceName(A)}`}</span>
          <span />
          <span>{`${ctx.letter(B)} · ${ctx.sourceName(B)}`}</span>
          <span>{mode === "runs" ? t_i18n("Verdict") : ctx.comparator ? t_i18n("vs {label}", { label: ctx.comparator.name }) : ""}</span>
        </div>
      )}
      {hunks.map((h) => (
        <section key={h.key} aria-label={h.title} className="space-y-1">
          <h4 className="flex justify-between gap-2 rounded-sm bg-elevation-hover px-2 py-1 content-compact font-mono-plex">
            <span>@@ {h.title} @@</span>
            <span className="tabular-nums text-default-secondary">{h.head}</span>
          </h4>
          {h.rows.map((it) => <DiffRow key={it.g.id} ctx={ctx} it={it} layout={layout} />)}
        </section>
      ))}
      {relRows.length > 0 && (
        <section aria-label={t_i18n("relationships")} className="space-y-1">
          <h4 className="flex justify-between gap-2 rounded-sm bg-elevation-hover px-2 py-1 content-compact font-mono-plex">
            <span>@@ {t_i18n("relationships")} @@</span>
            <span className="tabular-nums text-default-secondary">
              {mode === "runs"
                ? t_i18n("F1 {from} → {to}", { from: formatScore(f1(relationshipCounts(model, A, whatIf))), to: formatScore(f1(relationshipCounts(model, B, whatIf))) })
                : t_i18n("F1 {value}", { value: formatScore(f1(relationshipCounts(model, B, whatIf))) })}
            </span>
          </h4>
          {relRows.map((it) => <DiffRow key={it.r.id} ctx={ctx} it={it} layout={layout} />)}
        </section>
      )}
      {hunks.length === 0 && relRows.length === 0 && <p className="py-6 text-center content-compact text-default-secondary">{t_i18n("Nothing matches the filters.")}</p>}
    </div>
  );
}
