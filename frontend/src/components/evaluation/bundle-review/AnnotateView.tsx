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

// Annotate: the text the agent read, page by page, each mention of an object marked with
// its status (its name, an alias, its ATT&CK id, a hash, a defanged or refanged value),
// a margin of the objects each page names, and the objects the text never names.

import { Icon } from "@filigran/design-system";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { EXPECTED, displayStatus, entityVisible, type EntityItem, type Mention, type ObjectGroup } from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { MENTION_LABELS, STATUS_SURFACE, STATUS_TEXT, statusLabel } from "./labels";
import { isSelected, onActivate, type ReviewContext } from "./context";

export type AnnotateLens = "a" | "both" | "b";

const ReportGlyph = ({ className }: { className?: string }) => <Icon name="file-text" size={24} className={className} />;

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
        return (
          <span
            key={s}
            role="img"
            aria-label={label}
            title={label}
            className={cn(
              "inline-flex h-4 w-4 items-center justify-center rounded-sm border content-caption font-semibold",
              state === "on" ? cn(STATUS_SURFACE.tp, STATUS_TEXT.tp) : state === "near" ? cn(STATUS_SURFACE.near, STATUS_TEXT.near) : "border-elevation-subtle text-default-disabled",
            )}
          >
            {ctx.letter(s)}
          </span>
        );
      })}
    </span>
  );
}

function MentionMark({ ctx, m, focusable }: { ctx: ReviewContext; m: Mention; focusable: boolean }) {
  const { t_i18n } = useFormatter();
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
      title={label}
      onClick={select}
      onKeyDown={onActivate(select)}
      className={cn(
        "cursor-pointer rounded-sm border px-0.5 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        STATUS_SURFACE[st],
        !it.b && ctx.mode !== "alone" && "border-dashed bg-transparent",
        !entityVisible(it, ctx.filters, ctx.mode) && "opacity-40",
        isSelected(ctx, "e", m.gid) && "ring-2 ring-accent",
      )}
    >
      {m.text}
    </span>
  );
}

function ItemButton({ ctx, it, srcs }: { ctx: ReviewContext; it: EntityItem; srcs: string[] }) {
  const { t_i18n } = useFormatter();
  const st = displayStatus(it.st, ctx.mode, ctx.whatIf);
  return (
    <button
      type="button"
      aria-pressed={isSelected(ctx, "e", it.g.id)}
      onClick={() => ctx.select({ kind: "e", id: it.g.id })}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-sm border-l-2 px-1.5 py-0.5 text-left content-compact focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        STATUS_SURFACE[st],
        !entityVisible(it, ctx.filters, ctx.mode) && "opacity-40",
        isSelected(ctx, "e", it.g.id) && "ring-2 ring-accent",
      )}
    >
      <Who ctx={ctx} g={it.g} srcs={srcs} />
      <span className="min-w-0 flex-1 truncate">{itemName(it)}</span>
      <span className="sr-only">{t_i18n(statusLabel(st, ctx.mode))}</span>
    </button>
  );
}

export function AnnotateView({ ctx, lens, onLens }: { ctx: ReviewContext; lens: AnnotateLens; onLens: (lens: AnnotateLens) => void }) {
  const { t_i18n } = useFormatter();
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
  const shows = (srcs: string[]) => {
    const pa = mode !== "alone" && srcs.includes(pair.ref);
    const pb = srcs.includes(pair.under);
    return effective === "a" ? pa : effective === "b" ? pb : pa || pb;
  };
  const lost = derived.entItems.filter((it) => !located.mentions.has(it.g.id));
  const locatedCount = derived.entItems.length - lost.length;
  const source =
    review.entry.text_source === "file"
      ? t_i18n("text layer of {file}", { file: review.entry.file_name ?? "" })
      : t_i18n("prompt given to the agent");
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="content-compact text-default-secondary">
          {mode === "alone"
            ? t_i18n("What the answer extracted, marked on the report and checked against the expected bundle.")
            : t_i18n("Each mark is one mention; the margin says which source has the object: found, under another name, or not found.")}
        </p>
        {mode !== "alone" && (
          <SegmentedFilter
            selection="radio"
            size="sm"
            ariaLabel={t_i18n("Lens")}
            value={lens}
            onChange={onLens}
            options={[
              { key: "a", label: pair.ref === EXPECTED ? "Expected" : ctx.letter(pair.ref) },
              { key: "both", label: "Both" },
              { key: "b", label: ctx.letter(pair.under) },
            ]}
          />
        )}
      </div>
      <p className="flex flex-wrap justify-between gap-2 rounded-sm bg-elevation-hover px-2 py-1 content-caption text-default-secondary">
        <span>
          {t_i18n("{count, plural, one {# page} other {# pages}}", { count: located.pages.length })} · {source}
        </span>
        <span>{t_i18n("{located} of {total} objects located", { located: locatedCount, total: derived.entItems.length })}</span>
      </p>
      {located.pages.map((segments, pi) => {
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
          return <MentionMark key={si} ctx={ctx} m={seg} focusable={first} />;
        });
        return (
          <section
            key={pi}
            aria-label={t_i18n("Page {page} of {count}", { page: pi + 1, count: located.pages.length })}
            className="grid gap-3 lg:grid-cols-[1fr_14rem]"
          >
            <div className="rounded-sm border border-elevation-subtle bg-elevation-default p-4">
              <p className="whitespace-pre-wrap break-words content-compact leading-relaxed">{body}</p>
              <p className="mt-2 text-right content-caption text-default-secondary">
                {pi + 1} / {located.pages.length}
              </p>
            </div>
            {margin.length > 0 && (
              <ul className="space-y-1 self-start" aria-label={t_i18n("Objects on page {page}", { page: pi + 1 })}>
                {margin.map(({ it, srcs }) => (
                  <li key={it.g.id}>
                    <ItemButton ctx={ctx} it={it} srcs={srcs} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
      {lost.length > 0 && (
        <section aria-label={t_i18n("Not located in the report text")} className="space-y-1 rounded-sm border border-elevation-subtle p-3">
          <h4 className="content-compact font-medium">{t_i18n("Not located in the report text ({count})", { count: lost.length })}</h4>
          <p className="content-caption text-default-secondary">
            {t_i18n("No mention of their name, aliases, value or ATT&CK ID in the text. They still show in Coverage, Diff and the graphs.")}
          </p>
          <ul className="grid gap-1 md:grid-cols-2">
            {lost.map((it) => (
              <li key={it.g.id}>
                <ItemButton ctx={ctx} it={it} srcs={ctx.shown.filter((s) => it.g.by[s])} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
