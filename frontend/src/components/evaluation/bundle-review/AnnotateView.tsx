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

// Annotate: the text the agent read as pages, each mention of an object boxed with its
// status (its name, an alias, its ATT&CK id, a hash, a defanged or refanged value). A
// margin beside each page holds one card per object it names, at the height of its first
// mention and tied to it by a leader line; relationship arcs join the two ends of a
// relationship on the page; hovering a box or a card lights every box of that object.
// The lens shows one side, both, or the two side by side; the objects the text never
// names are listed under the pages.

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  displayStatus,
  entityVisible,
  relationshipVisible,
  type DisplayStatus,
  type EntityItem,
  type Mention,
  type ObjectGroup,
  type RelationshipItem,
} from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { MENTION_LABELS, STATUS_COLOR, STATUS_SURFACE, STATUS_TEXT, statusLabel } from "./labels";
import { edgeLabel } from "./graph-text";
import { isSelected, onActivate, type ReviewContext } from "./context";

export type AnnotateLens = "a" | "both" | "split" | "b";
/** Which relationship arcs the pages draw: the selected object's (or the selected relationship), every visible one, or none. */
type AnnotateArcs = "selected" | "all" | "none";

/** One page drawn with one side's mentions: the reference's, the answer's, or both. */
type PageLens = "a" | "both" | "b";

const ReportGlyph = ({ className }: { className?: string }) => <Icon name="file-text" size={24} className={className} />;
const PAPER = "var(--bg-elevation-default)";
/** A margin card's height and the gap between two cards, in pixels. */
const CARD_H = 26;
const CARD_GAP = 4;

function itemName(it: EntityItem): string {
  return (it.b ?? it.a ?? it.g.by[EXPECTED] ?? Object.values(it.g.all)[0][0]).name;
}

/** One letter per source on screen: found here, found under another name, or not found. */
function Who({ ctx, g, srcs }: { ctx: ReviewContext; g: ObjectGroup; srcs: string[] }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex shrink-0 gap-0.5">
      {ctx.shown.map((s) => {
        const state = srcs.includes(s) ? "on" : g.by[s] ? "near" : "off";
        const label =
          state === "on"
            ? t_i18n("{source}: found here", { source: ctx.letter(s) })
            : state === "near"
              ? t_i18n("{source}: as “{name}”", { source: ctx.letter(s), name: g.by[s].name })
              : t_i18n("{source}: not found", { source: ctx.letter(s) });
        return <WhoBadge key={s} state={state} letter={ctx.letter(s)} label={label} />;
      })}
    </span>
  );
}

function WhoBadge({ state, letter, label }: { state: "on" | "near" | "off"; letter: string; label: string }) {
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex h-4 min-w-4 items-center justify-center rounded-sm border px-0.5 content-caption font-semibold",
        state === "on"
          ? cn(STATUS_SURFACE.tp, STATUS_TEXT.tp)
          : state === "near"
            ? cn(STATUS_SURFACE.near, STATUS_TEXT.near)
            : "border-dashed border-elevation-subtle text-default-disabled line-through",
      )}
    >
      {letter}
    </span>
  );
}

/** A status pill: the status's words on its tint. */
function StatusPill({ st, label }: { st: DisplayStatus; label: string }) {
  return <span className={cn("ml-auto shrink-0 rounded-sm border px-1.5 content-caption", STATUS_SURFACE[st], STATUS_TEXT[st])}>{label}</span>;
}

/** What a box's hover says: the type, the status, how it was found, and who has it. */
function useMentionTitle(ctx: ReviewContext) {
  const { t_i18n } = useFormatter();
  return (it: EntityItem, st: DisplayStatus, m: Mention) =>
    [
      it.g.type,
      t_i18n(statusLabel(st, ctx.mode)),
      t_i18n("located by {how}", { how: t_i18n(MENTION_LABELS[m.how]) }),
      ...ctx.shown.map((s) => {
        const who = s === EXPECTED ? t_i18n("Expected") : ctx.sourceName(s);
        const has = m.srcs.includes(s) ? t_i18n("yes") : it.g.by[s] ? t_i18n("as “{name}”", { name: it.g.by[s].name }) : t_i18n("no");
        return `${who}: ${has}`;
      }),
    ].join(" · ");
}

function MentionMark({
  ctx,
  m,
  focusable,
  hot,
  onHot,
}: {
  ctx: ReviewContext;
  m: Mention;
  focusable: boolean;
  hot: boolean;
  onHot: (gid: string | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const titleOf = useMentionTitle(ctx);
  const it = ctx.derived.ents.get(m.gid);
  if (!it) return <>{m.text}</>;
  const st = displayStatus(it.st, ctx.mode, ctx.whatIf);
  const select = () => ctx.select({ kind: "e", id: m.gid });
  const label = t_i18n("{type} {name}, {status}, located by {how}", {
    type: it.g.type,
    name: itemName(it),
    status: t_i18n(statusLabel(st, ctx.mode)),
    how: t_i18n(MENTION_LABELS[m.how]),
  });
  return (
    <span
      role="button"
      tabIndex={focusable ? 0 : -1}
      aria-pressed={isSelected(ctx, "e", m.gid)}
      aria-label={label}
      title={titleOf(it, st, m)}
      data-gid={m.gid}
      data-srcs={m.srcs.join(" ")}
      onClick={select}
      onKeyDown={onActivate(select)}
      onMouseEnter={() => onHot(m.gid)}
      onMouseLeave={() => onHot(null)}
      className={cn(
        "cursor-pointer rounded-sm border-b-2 px-0.5 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        STATUS_SURFACE[st],
        !it.b && ctx.mode !== "alone" && "border border-dashed bg-transparent",
        !entityVisible(it, ctx.filters, ctx.mode) && "opacity-40",
        hot && "ring-1 ring-accent",
        isSelected(ctx, "e", m.gid) && "ring-2 ring-accent",
      )}
    >
      {m.text}
    </span>
  );
}

interface PageLayout {
  cards: Record<string, number>;
  marginHeight: number;
  leaders: { gid: string; d: string }[];
  arcs: { id: string; d: string; x2: number; y2: number; lx: number; ly: number; label: string; st: DisplayStatus; gone: boolean }[];
}

const EMPTY_LAYOUT: PageLayout = { cards: {}, marginHeight: 0, leaders: [], arcs: [] };

/** The relationships whose arcs a page draws. */
function arcItems(ctx: ReviewContext, arcs: AnnotateArcs, lens: PageLens): RelationshipItem[] {
  let items = ctx.derived.relItems;
  if (arcs === "none") return [];
  if (arcs === "all") items = items.filter((it) => relationshipVisible(ctx.model, it, ctx.filters, ctx.mode));
  else if (ctx.selection?.kind === "e") items = items.filter((it) => it.r.src === ctx.selection?.id || it.r.tgt === ctx.selection?.id);
  else if (ctx.selection?.kind === "r") items = items.filter((it) => it.r.id === ctx.selection?.id);
  else return [];
  if (lens === "a") items = items.filter((it) => it.inA);
  if (lens === "b") items = items.filter((it) => it.inB);
  return items;
}

function PdfPage({
  ctx,
  segments,
  index,
  count,
  lens,
  withMargin,
  arcs,
  hot,
  onHot,
}: {
  ctx: ReviewContext;
  segments: ReviewContext["located"]["pages"][number];
  index: number;
  count: number;
  lens: PageLens;
  withMargin: boolean;
  arcs: AnnotateArcs;
  hot: string | null;
  onHot: (gid: string | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const { mode, pair } = ctx;
  const pageRef = useRef<HTMLDivElement>(null);
  const sheetRef = useRef<HTMLDivElement>(null);
  const marginRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<PageLayout>(EMPTY_LAYOUT);
  const [tick, setTick] = useState(0);

  const shows = (srcs: string[]) => {
    const pa = mode !== "alone" && srcs.includes(pair.ref);
    const pb = srcs.includes(pair.under);
    return lens === "a" ? pa : lens === "b" ? pb : pa || pb;
  };
  const seen = new Set<string>();
  const margin: { it: EntityItem; srcs: string[] }[] = [];
  const body = segments.map((seg, si) => {
    if (typeof seg === "string") return seg;
    if (!ctx.derived.ents.has(seg.gid) || !shows(seg.srcs)) return seg.text;
    const first = !seen.has(seg.gid);
    if (first) {
      seen.add(seg.gid);
      margin.push({ it: ctx.derived.ents.get(seg.gid) as EntityItem, srcs: seg.srcs });
    }
    return <MentionMark key={si} ctx={ctx} m={seg} focusable={first} hot={hot === seg.gid} onHot={onHot} />;
  });
  const items = arcItems(ctx, arcs, lens);
  const arcsOn = items.length > 0;

  // Redraw on resize: cards, leader lines and arcs follow the text's wrapping.
  useLayoutEffect(() => {
    const page = pageRef.current;
    if (!page || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setTick((n) => n + 1));
    observer.observe(page);
    return () => observer.disconnect();
  }, []);

  const measure = useCallback((): PageLayout => {
    const page = pageRef.current;
    const sheet = sheetRef.current;
    if (!page || !sheet) return EMPTY_LAYOUT;
    const pb = page.getBoundingClientRect();
    const sb = sheet.getBoundingClientRect();
    const marks = [...sheet.querySelectorAll<HTMLElement>("[data-gid]")];
    // Each object's first mention: its middle height and where its box ends.
    const first = new Map<string, { y: number; x: number }>();
    for (const mark of marks) {
      const gid = mark.dataset.gid as string;
      if (first.has(gid)) continue;
      const rects = [...mark.getClientRects()].filter((r) => r.width > 0);
      if (!rects.length) continue;
      first.set(gid, { y: rects[0].top - pb.top + rects[0].height / 2, x: rects[rects.length - 1].right - pb.left + 2 });
    }
    const cards: Record<string, number> = {};
    const leaders: PageLayout["leaders"] = [];
    let bottom = 0;
    const mb = marginRef.current?.getBoundingClientRect();
    const sheetRight = sb.right - pb.left;
    [...first.entries()]
      .sort((a, b) => a[1].y - b[1].y)
      .forEach(([gid, f]) => {
        const top = Math.max(f.y - CARD_H / 2, bottom + CARD_GAP);
        bottom = top + CARD_H;
        cards[gid] = top;
        if (mb && mb.width > 0) leaders.push({ gid, d: `M${f.x},${f.y} H${sheetRight + 4} L${mb.left - pb.left},${top + CARD_H / 2}` });
      });
    const arcsOut: PageLayout["arcs"] = [];
    items.forEach((it, n) => {
      const side = lens === "a" ? pair.ref : lens === "b" ? pair.under : it.inB ? pair.under : pair.ref;
      const find = (gid: string) => marks.find((m) => m.dataset.gid === gid && (m.dataset.srcs ?? "").split(" ").includes(side));
      const a = find(it.r.src);
      const b = find(it.r.tgt);
      const ra = a?.getClientRects()[0];
      const rb = b?.getClientRects()[0];
      if (!ra || !rb) return;
      const x1 = ra.left + ra.width / 2 - sb.left, y1 = ra.top - sb.top - 2, x2 = rb.left + rb.width / 2 - sb.left, y2 = rb.top - sb.top - 2;
      const h = 14 + (n % 3) * 7 + Math.min(12, Math.abs(x2 - x1) * 0.03);
      const top = Math.min(y1, y2) - h;
      arcsOut.push({
        id: it.r.id,
        d: `M${x1},${y1} C${x1},${top} ${x2},${top} ${x2},${y2}`,
        x2,
        y2,
        lx: (x1 + x2) / 2,
        ly: (y1 + 6 * top + y2) / 8,
        label: edgeLabel(ctx, it, lens, t_i18n),
        st: displayStatus(it.st, mode, ctx.whatIf),
        gone: !it.inB && mode !== "alone",
      });
    });
    return { cards, marginHeight: bottom, leaders, arcs: arcsOut };
  }, [ctx, items, lens, mode, pair, t_i18n]);

  useLayoutEffect(() => {
    const next = measure();
    setLayout((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
    // `tick` redraws after a resize.
  }, [measure, tick]);

  return (
    <section
      ref={pageRef}
      aria-label={t_i18n("Page {page} of {count}", { page: index + 1, count })}
      className={cn("relative mx-auto grid w-full max-w-5xl items-start gap-6", withMargin && "lg:grid-cols-[minmax(0,1fr)_13rem]")}
    >
      <div ref={sheetRef} className="relative min-h-96 rounded-sm border border-elevation-subtle bg-elevation-default px-8 pb-10 pt-8">
        <p className={cn("whitespace-pre-wrap break-words content-compact", arcsOn ? "leading-loose pt-4" : "leading-relaxed")}>{body}</p>
        <p className="absolute bottom-3 right-4 content-caption text-default-secondary tabular-nums">
          {index + 1} / {count}
        </p>
        {layout.arcs.length > 0 && (
          <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" aria-hidden>
            {layout.arcs.map((arc) => {
              const color = STATUS_COLOR[arc.st];
              const dashed = arc.gone || arc.st === "fn" || arc.st === "cascade" || arc.st === "sim";
              const w = arc.label.length * 6 + 10;
              return (
                <g key={arc.id}>
                  <path d={arc.d} fill="none" strokeWidth={1.6} strokeDasharray={dashed ? "5 3" : undefined} style={{ stroke: color }} />
                  <circle cx={arc.x2} cy={arc.y2} r={2.4} style={{ fill: color }} />
                  {arc.label && (
                    <>
                      <rect x={arc.lx - w / 2} y={arc.ly - 8} width={w} height={16} rx={3} strokeWidth={1} style={{ fill: PAPER, stroke: color }} />
                      <text x={arc.lx} y={arc.ly + 4} textAnchor="middle" className="content-caption font-mono" style={{ fill: color }}>
                        {arc.label}
                      </text>
                    </>
                  )}
                </g>
              );
            })}
          </svg>
        )}
      </div>
      {withMargin && margin.length > 0 && (
        <>
          <div
            ref={marginRef}
            className="relative hidden lg:block"
            style={{ minHeight: layout.marginHeight || margin.length * (CARD_H + CARD_GAP) }}
            aria-label={t_i18n("Objects on page {page}", { page: index + 1 })}
            role="list"
          >
            {margin.map(({ it, srcs }, i) => {
              const st = displayStatus(it.st, mode, ctx.whatIf);
              const gid = it.g.id;
              return (
                <div key={gid} role="listitem" className="absolute inset-x-0" style={{ top: layout.cards[gid] ?? i * (CARD_H + CARD_GAP) }}>
                  <button
                    type="button"
                    aria-pressed={isSelected(ctx, "e", gid)}
                    onClick={() => ctx.select({ kind: "e", id: gid })}
                    onMouseEnter={() => onHot(gid)}
                    onMouseLeave={() => onHot(null)}
                    title={`${it.g.type} · ${t_i18n(statusLabel(st, mode))}`}
                    className={cn(
                      "flex h-6.5 w-full items-center gap-1.5 overflow-hidden rounded-sm border border-l-4 bg-elevation-default px-1.5 text-left content-caption focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                      STATUS_SURFACE[st],
                      !it.b && mode !== "alone" && "border-dashed",
                      !entityVisible(it, ctx.filters, mode) && "opacity-40",
                      hot === gid && "ring-1 ring-accent",
                      isSelected(ctx, "e", gid) && "ring-2 ring-accent",
                    )}
                  >
                    {lens === "both" ? (
                      <Who ctx={ctx} g={it.g} srcs={srcs} />
                    ) : (
                      <span aria-hidden className={cn("h-2 w-2 shrink-0 rounded-full border", STATUS_SURFACE[st])} />
                    )}
                    <span className="min-w-0 flex-1 truncate">{itemName(it)}</span>
                    <span className="sr-only">{t_i18n(statusLabel(st, mode))}</span>
                  </button>
                </div>
              );
            })}
          </div>
          {layout.leaders.length > 0 && (
            <svg className="pointer-events-none absolute inset-0 hidden h-full w-full overflow-visible lg:block" aria-hidden>
              {layout.leaders.map((leader) => {
                const it = ctx.derived.ents.get(leader.gid);
                const st = it ? displayStatus(it.st, mode, ctx.whatIf) : "same";
                const selected = isSelected(ctx, "e", leader.gid) || hot === leader.gid;
                return (
                  <path
                    key={leader.gid}
                    d={leader.d}
                    fill="none"
                    strokeWidth={selected ? 1.5 : 1}
                    strokeDasharray={selected ? undefined : "2 3"}
                    style={{ stroke: STATUS_COLOR[st], opacity: selected ? 1 : 0.6 }}
                  />
                );
              })}
            </svg>
          )}
        </>
      )}
    </section>
  );
}

export function AnnotateView({
  ctx,
  lens,
  onLens,
  arcs: arcsProp,
  onArcs,
}: {
  ctx: ReviewContext;
  lens: AnnotateLens;
  onLens: (lens: AnnotateLens) => void;
  /** The arcs drawn; the view keeps its own choice when the page does not. */
  arcs?: AnnotateArcs;
  onArcs?: (arcs: AnnotateArcs) => void;
}) {
  const { t_i18n } = useFormatter();
  const [ownArcs, setOwnArcs] = useState<AnnotateArcs>("selected");
  const [hot, setHot] = useState<string | null>(null);
  const arcs = arcsProp ?? ownArcs;
  const setArcs = onArcs ?? setOwnArcs;
  const { review, located, derived, mode, pair } = ctx;
  if (!review.entry.pages.length) {
    return (
      <EmptyState
        variant="inset"
        icon={ReportGlyph}
        title="No report text"
        description="This entry has no prompt text and no attached file with a readable text layer, so its objects cannot be located in it. Coverage, Diff and the graphs still show them."
      />
    );
  }
  const effective: AnnotateLens = mode === "alone" ? "b" : lens;
  const lost = derived.entItems.filter((it) => !located.mentions.has(it.g.id));
  const locatedCount = derived.entItems.length - lost.length;
  const refName = pair.ref === EXPECTED ? t_i18n("Expected") : ctx.sourceName(pair.ref);
  const underName = ctx.sourceName(pair.under);
  const readers = [pair.ref, pair.under].filter((s) => review.answers.some((a) => a.key === s)).map((s) => ctx.sourceName(s));
  const source =
    review.entry.text_source === "file"
      ? readers.length
        ? t_i18n("PDF text layer; {names} read the file itself", {
            names: readers.length > 1 ? t_i18n("{a} and {b}", { a: readers[0], b: readers[1] }) : readers[0],
          })
        : t_i18n("PDF text layer")
      : t_i18n("text layer as given to the agent");
  const sources = ctx.shown
    .map((s) => `${ctx.letter(s)} ${s === EXPECTED ? t_i18n("expected bundle") : ctx.sourceName(s)}`)
    .join(", ");
  const help =
    mode === "alone" ? (
      t_i18n("What {name} extracted, boxed on the report and checked against the expected bundle.", { name: underName })
    ) : effective === "both" ? (
      <>
        {t_i18n("Each box is one mention; the margin says who has it ({sources}):", { sources })}{" "}
        <span className="inline-flex flex-wrap items-center gap-1.5 align-middle">
          <WhoBadge state="on" letter="E" label={t_i18n("found")} /> {t_i18n("found")} ·
          <WhoBadge state="near" letter="E" label={t_i18n("under another name")} /> {t_i18n("under another name")} ·
          <WhoBadge state="off" letter="E" label={t_i18n("not found")} /> {t_i18n("not found")}
        </span>
      </>
    ) : effective === "split" ? (
      t_i18n("The same report twice: {a} on the left, {b} on the right.", { a: refName, b: underName })
    ) : (
      t_i18n("Only what {name} has, boxed on the report.", { name: effective === "a" ? refName : underName })
    );
  const page = (pageLens: PageLens, withMargin: boolean) =>
    located.pages.map((segments, pi) => (
      <PdfPage
        key={`${pageLens}-${pi}`}
        ctx={ctx}
        segments={segments}
        index={pi}
        count={located.pages.length}
        lens={pageLens}
        withMargin={withMargin}
        arcs={arcs}
        hot={hot}
        onHot={setHot}
      />
    ));
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 flex-1 content-compact text-default-secondary">{help}</p>
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedFilter
            selection="radio"
            size="sm"
            ariaLabel={t_i18n("Relationship arcs")}
            value={arcs}
            onChange={setArcs}
            options={[
              { key: "selected", label: "Arcs: selected" },
              { key: "all", label: "All" },
              { key: "none", label: "None" },
            ]}
          />
          {mode !== "alone" && (
            <SegmentedFilter
              selection="radio"
              size="sm"
              ariaLabel={t_i18n("Lens")}
              value={lens}
              onChange={onLens}
              options={[
                { key: "a", label: pair.ref === EXPECTED ? "Expected" : ctx.letter(pair.ref), hint: refName },
                { key: "both", label: "Both" },
                { key: "split", label: "Side by side" },
                { key: "b", label: ctx.letter(pair.under), hint: underName },
              ]}
            />
          )}
        </div>
      </div>
      <p className="flex flex-wrap gap-x-4 gap-y-1 rounded-sm bg-elevation-hover px-3 py-1.5 content-caption text-default-secondary">
        {review.entry.file_name && <span className="font-mono text-default-primary">{review.entry.file_name}</span>}
        <span>
          {t_i18n("{count, plural, one {# page} other {# pages}}", { count: located.pages.length })} · {source}
        </span>
        <span className="tabular-nums">
          {t_i18n("{located} of {total} objects located", { located: locatedCount, total: derived.entItems.length })}
        </span>
      </p>
      <div className="space-y-5 rounded-sm bg-elevation-hover p-4">
        {effective === "split" ? (
          <div className="grid gap-4 md:grid-cols-2">
            {(["a", "b"] as const).map((side) => (
              <div key={side} className="min-w-0 space-y-4">
                <p className="content-caption font-semibold uppercase text-default-secondary">
                  {side === "a"
                    ? pair.ref === EXPECTED
                      ? t_i18n("Expected")
                      : `${ctx.letter(pair.ref)} · ${refName}`
                    : `${ctx.letter(pair.under)} · ${underName}`}
                </p>
                {page(side, false)}
              </div>
            ))}
          </div>
        ) : (
          page(effective, true)
        )}
        {lost.length > 0 && (
          <section
            aria-label={t_i18n("Not located in the document")}
            className="mx-auto w-full max-w-5xl space-y-1 rounded-sm border border-elevation-subtle bg-elevation-default p-3"
          >
            <h4 className="content-compact font-medium">{t_i18n("Not located in the document ({count})", { count: lost.length })}</h4>
            <p className="content-caption text-default-secondary">
              {t_i18n("No mention of their name, aliases, value or ATT&CK ID in the text layer. They still show in Coverage, Diff and the graphs.")}
            </p>
            <ul className="space-y-1">
              {lost.map((it) => {
                const st = displayStatus(it.st, mode, ctx.whatIf);
                return (
                  <li key={it.g.id}>
                    <button
                      type="button"
                      aria-pressed={isSelected(ctx, "e", it.g.id)}
                      onClick={() => ctx.select({ kind: "e", id: it.g.id })}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-sm border border-l-4 bg-elevation-default px-2 py-1 text-left content-compact focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                        STATUS_SURFACE[st],
                        !entityVisible(it, ctx.filters, mode) && "opacity-40",
                        isSelected(ctx, "e", it.g.id) && "ring-2 ring-accent",
                      )}
                    >
                      <Who ctx={ctx} g={it.g} srcs={ctx.shown.filter((s) => it.g.by[s])} />
                      <span className="shrink-0 font-mono content-caption text-default-secondary">{it.g.type}</span>
                      <span className="min-w-0 truncate">{itemName(it)}</span>
                      <StatusPill st={st} label={t_i18n(statusLabel(st, mode))} />
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
