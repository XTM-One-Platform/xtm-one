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

// The inspector: the selected object or relationship in the expected bundle and in the
// compared answers side by side, and why the scorer counted it as it did.

import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  compareEntity,
  compareRelationship,
  displayStatus,
  endName,
  entityVerdict,
  nearKind,
  relationshipOk,
  relationshipVerdict,
  tripleText,
  type ObjectGroup,
  type RelationshipItem,
  type RelationshipRow,
  type StixObject,
} from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { MENTION_LABELS, NEAR_KIND_LABELS, STATUS_SURFACE, STATUS_TEXT, statusLabel } from "./labels";
import { noteFor } from "./graph-text";
import type { ReviewContext } from "./context";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** ✓ found as expected, ≈ found in another form, ✗ not expected. */
export function OkMark({ ctx, row, group, source }: { ctx: ReviewContext; row?: RelationshipRow; group?: ObjectGroup; source: string }) {
  const { t_i18n } = useFormatter();
  const st = row ? compareRelationship(ctx.model, row, EXPECTED, source)?.st : group ? compareEntity(group, EXPECTED, source) : null;
  const sim = ctx.whatIf && (st === "near" || st === "cascade");
  const [mark, label, tone] =
    st === "tp" || sim
      ? ["✓", t_i18n("In the expected bundle"), STATUS_TEXT.tp]
      : st === "near" || st === "cascade" || st === "type"
        ? ["≈", t_i18n("In the expected bundle in another form"), STATUS_TEXT.near]
        : ["✗", t_i18n("Not in the expected bundle"), STATUS_TEXT.fp];
  return (
    <span role="img" aria-label={label} title={label} className={cn("mr-1 font-semibold", tone)}>
      {mark}
    </span>
  );
}

function StatusPill({ label, status }: { label: string; status: keyof typeof STATUS_SURFACE }) {
  return (
    <span className={cn("inline-flex items-center rounded-sm border px-2 py-0.5 content-caption font-medium text-default-primary", STATUS_SURFACE[status])}>
      {label}
    </span>
  );
}

function TypeChip({ children }: { children: string }) {
  return <span className="rounded-sm bg-elevation-hover px-1.5 py-0.5 content-caption font-mono-plex text-default-secondary">{children}</span>;
}

function sourceHead(ctx: ReviewContext, s: string, t_i18n: Translate) {
  return s === EXPECTED ? t_i18n("Expected") : `${ctx.letter(s)} · ${ctx.sourceName(s)}`;
}

function whyEntity(ctx: ReviewContext, g: ObjectGroup, s: string, t_i18n: Translate): string {
  const st = compareEntity(g, EXPECTED, s);
  const a = g.by[EXPECTED];
  const b = g.by[s];
  const extra = g.extra[s]
    ? ` ${t_i18n("It also produced {count, plural, one {# more object} other {# more objects}} for this one, each counted as spurious.", { count: g.extra[s] })}`
    : "";
  if (!st) return "";
  if (st === "fn") return t_i18n("Not in this answer.");
  if (st === "fp") return t_i18n("Not in the expected bundle.") + extra;
  if (st === "tp") return extra ? t_i18n("Same canonical ID as the expected bundle.") + extra : "";
  const kind = nearKind(a, b);
  const text =
    kind === "alias"
      ? t_i18n("Named “{answer}”, an alias of the expected “{expected}”. The canonical ID is derived from the name, so it counts as 1 missing + 1 spurious object.", { answer: b.name, expected: a.name })
      : kind === "type"
        ? t_i18n("Typed {answer} where the expected bundle has {expected}: another object type, so another canonical ID.", { answer: b.type, expected: a.type })
        : kind === "defanged"
          ? t_i18n("Kept the defanged form “{answer}”; the expected bundle has “{expected}”. The canonical ID is not refanged.", { answer: b.name, expected: a.name })
          : kind === "attack-id"
            ? t_i18n("Same technique ({id}) under another name: “{answer}” where the expected bundle has “{expected}”.", { id: a.ext ?? "", answer: b.name, expected: a.name })
            : t_i18n("Named “{answer}” where the expected bundle has “{expected}”. Different names give different canonical IDs, so it counts as 1 missing + 1 spurious object.", { answer: b.name, expected: a.name });
  return text + (ctx.whatIf ? ` ${t_i18n("The what-if counts it as a match.")}` : "") + extra;
}

const FIELDS = ["name", "type", "canonical id", "aliases", "ATT&CK ID", "hashes"] as const;
/** The inspector's field names, read by `t_i18n`. @i18n-keys */
const FIELD_LABELS = {
  name: "Name",
  type: "Type",
  "canonical id": "Canonical ID",
  aliases: "Aliases",
  "ATT&CK ID": "ATT&CK ID",
  hashes: "Hashes",
} as const;

function fieldValue(o: StixObject | undefined, key: (typeof FIELDS)[number]): string | null {
  if (!o) return null;
  if (key === "name") return o.name;
  if (key === "type") return o.type;
  if (key === "canonical id") return o.cid;
  if (key === "aliases") return o.aliases.join(", ") || null;
  if (key === "ATT&CK ID") return o.ext;
  return o.hashes ? Object.entries(o.hashes).map(([a, h]) => `${a} ${h}`).join(" · ") : null;
}

/** How the answer's agent and model did on this object in each of their runs on this
 *  report: "matched → missing → …", and what changed since the previous run. */
function HistoryLine({ ctx, kind, id }: { ctx: ReviewContext; kind: "e" | "r"; id: string }) {
  const { t_i18n } = useFormatter();
  const h = ctx.history;
  if (!h || h.runs.length < 2) return null;
  const g = kind === "e" ? ctx.fullModel.byId.get(id) : undefined;
  const r = kind === "r" ? ctx.fullModel.relById.get(id) : undefined;
  if (!g && !r) return null;
  const said = (key: string) => {
    if (ctx.review.unreadable.includes(key)) return t_i18n("no readable answer");
    const st = g ? compareEntity(g, EXPECTED, key) : r ? compareRelationship(ctx.fullModel, r, EXPECTED, key)?.st : null;
    return st ? t_i18n(statusLabel(st, "score")).toLowerCase() : t_i18n("absent");
  };
  const B = ctx.pair.under;
  const prev = ctx.comparator && ctx.mode !== "runs" ? ctx.comparator.key : null;
  const verdict = prev ? (g ? entityVerdict(g, prev, B, ctx.whatIf) : r ? relationshipVerdict(ctx.fullModel, r, prev, B, ctx.whatIf) : null) : null;
  return (
    <p aria-label={t_i18n("History of this object")} className="flex flex-wrap items-center gap-x-1.5 gap-y-1 content-caption text-default-secondary">
      <span className="font-mono-plex text-default-primary">{h.label}</span>
      {h.runs.map((run, i) => (
        <span key={run.key} className="inline-flex items-center gap-1.5">
          {i > 0 && <span aria-hidden>→</span>}
          <span className={cn(run.key === B && "font-semibold text-default-primary")}>
            {run.code}: {said(run.key)}
          </span>
        </span>
      ))}
      {verdict && verdict !== "same" && (
        <span className={cn("rounded-sm border px-2 text-default-primary", STATUS_SURFACE[verdict])}>{t_i18n(statusLabel(verdict, "runs"))}</span>
      )}
    </p>
  );
}

function EntityInspector({ ctx, g }: { ctx: ReviewContext; g: ObjectGroup }) {
  const { t_i18n } = useFormatter();
  const it = ctx.derived.ents.get(g.id);
  const st = it ? displayStatus(it.st, ctx.mode, ctx.whatIf) : "same";
  const near = it && it.a && it.b && it.a.cid !== it.b.cid ? nearKind(it.a, it.b) : it && ctx.mode === "alone" && it.st === "near" && g.by[EXPECTED] && it.b ? nearKind(g.by[EXPECTED], it.b) : null;
  const cols = ctx.shown;
  const names = [...new Set(cols.filter((s) => g.by[s]).map((s) => g.by[s].name))];
  const keys = FIELDS.filter((k) => k === "name" || k === "canonical id" || cols.some((s) => fieldValue(g.by[s], k)));
  const mentions = ctx.located.mentions.get(g.id);
  const pair = ctx.mode === "runs" ? [ctx.pair.ref, ctx.pair.under] : [ctx.pair.under];
  const whys = pair.map((s) => [s, whyEntity(ctx, g, s, t_i18n)] as const).filter(([, w]) => w);
  const rels = ctx.derived.relItems.filter((x) => x.r.src === g.id || x.r.tgt === g.id);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TypeChip>{g.type}</TypeChip>
        <StatusPill
          status={st}
          label={it ? `${t_i18n(statusLabel(st, ctx.mode))}${near ? ` · ${t_i18n(NEAR_KIND_LABELS[near])}` : ""}` : t_i18n("Not in this comparison")}
        />
      </div>
      <h3 className="content-base-bold break-words">
        {names.map((n, i) => (
          <span key={n} className={i ? "ml-1 text-default-secondary font-normal" : undefined}>
            {i ? `≈ ${n}` : n}
          </span>
        ))}
      </h3>
      <p className="content-compact text-default-secondary">
        {mentions
          ? ctx.review.entry.file_name
            ? t_i18n("{count, plural, one {# mention} other {# mentions}} in {file}, page {pages}, found by {hows}", {
                count: mentions.length,
                file: ctx.review.entry.file_name,
                pages: [...new Set(mentions.map((m) => m.page))].join(", "),
                hows: [...new Set(mentions.map((m) => t_i18n(MENTION_LABELS[m.how])))].join("; "),
              })
            : t_i18n("{count, plural, one {# mention} other {# mentions}} in the report, page {pages}, found by {hows}", {
                count: mentions.length,
                pages: [...new Set(mentions.map((m) => m.page))].join(", "),
                hows: [...new Set(mentions.map((m) => t_i18n(MENTION_LABELS[m.how])))].join("; "),
              })
          : ctx.review.entry.file_name
            ? t_i18n("Not located in {file}", { file: ctx.review.entry.file_name })
            : t_i18n("Not located in the report text")}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full content-compact">
          <thead>
            <tr>
              <th className="sr-only">{t_i18n("Field")}</th>
              {cols.map((s) => (
                <th key={s} scope="col" className="px-1.5 py-1 text-left font-medium text-default-secondary">
                  {sourceHead(ctx, s, t_i18n)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key} className="border-t border-elevation-subtle">
                <th scope="row" className="px-1.5 py-1 text-left font-medium text-default-secondary whitespace-nowrap align-top">
                  {t_i18n(FIELD_LABELS[key])}
                </th>
                {cols.map((s) => {
                  const o = g.by[s];
                  if (!o) {
                    return (
                      <td key={s} className="px-1.5 py-1 italic text-default-secondary align-top">
                        {s === EXPECTED ? t_i18n("not expected") : g.by[EXPECTED] ? t_i18n("missed") : t_i18n("absent")}
                      </td>
                    );
                  }
                  const v = fieldValue(o, key);
                  const differs = s !== EXPECTED && g.by[EXPECTED] && fieldValue(g.by[EXPECTED], key) != null && v !== fieldValue(g.by[EXPECTED], key);
                  return (
                    <td
                      key={s}
                      className={cn(
                        "px-1.5 py-1 align-top break-all",
                        (key === "canonical id" || key === "hashes") && "font-mono-plex",
                        differs && STATUS_TEXT.near,
                      )}
                    >
                      {key === "name" && s !== EXPECTED && <OkMark ctx={ctx} group={g} source={s} />}
                      {v ?? "—"}
                      {key === "name" && g.extra[s] ? <span className="ml-1 text-default-secondary">+{g.extra[s]}</span> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {whys.map(([s, w]) => (
        <p key={s} className={cn("rounded-sm border-l-2 px-2 py-1 content-compact", STATUS_SURFACE[st])}>
          <span className="font-semibold">
            {ctx.letter(s)} · {ctx.sourceName(s)}
          </span>{" "}
          {w}
        </p>
      ))}
      <HistoryLine ctx={ctx} kind="e" id={g.id} />
      {rels.length > 0 && (
        <div>
          <h4 className="content-caption font-medium uppercase tracking-wider text-default-secondary mb-1">
            {t_i18n("Relationships ({count})", { count: rels.length })}
          </h4>
          <ul className="space-y-1">
            {rels.map((x) => (
              <li key={x.r.id}>
                <button
                  type="button"
                  onClick={() => ctx.select({ kind: "r", id: x.r.id })}
                  className="flex w-full items-start gap-1.5 rounded-sm px-1 py-0.5 text-left content-compact hover:bg-elevation-hover focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <span aria-hidden className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", STATUS_TEXT[displayStatus(x.st, ctx.mode, ctx.whatIf)], "bg-current")} />
                  <span className={cn("break-words", !x.inB && "line-through text-default-secondary")}>
                    {tripleText(x.r, x.inB ? ctx.pair.under : ctx.pair.ref)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function whyRelationship(ctx: ReviewContext, it: RelationshipItem, t_i18n: Translate): string {
  const { r } = it;
  const { ref: A, under: B } = ctx.pair;
  const answer = ctx.letter(B);
  if (ctx.mode === "runs") {
    const say = (s: string) =>
      relationshipOk(ctx.model, r, s, ctx.whatIf) ? t_i18n("correct") : r.kinds[s] ? t_i18n("wrong") : r.kinds[EXPECTED] ? t_i18n("missing") : t_i18n("absent");
    const note = noteFor(ctx, it, t_i18n);
    const states = t_i18n("{a} ({nameA}): {stateA}. {b} ({nameB}): {stateB}.", {
      a: ctx.letter(A),
      nameA: ctx.sourceName(A),
      stateA: say(A),
      b: answer,
      nameB: ctx.sourceName(B),
      stateB: say(B),
    });
    return note ? `${states} ${t_i18n("Between the two: {note}.", { note })}` : states;
  }
  const c = it.cause ? ctx.model.byId.get(it.cause) : undefined;
  switch (it.pairSt) {
    case "tp":
      return t_i18n("Same canonical (source, type, target) triple as the expected bundle.");
    case "fn":
      return c
        ? t_i18n("Missing from {answer} because its endpoint, the {type} “{name}”, is missing too.", { answer, type: c.type, name: endName(c, EXPECTED) })
        : t_i18n("Missing from {answer}.", { answer });
    case "fp":
      return c
        ? t_i18n("Not expected: its endpoint, the {type} “{name}”, is not in the expected bundle.", { type: c.type, name: endName(c, B) })
        : t_i18n("Not in the expected bundle.");
    case "cascade":
      return c
        ? t_i18n("{answer} wrote this relationship, but its endpoint “{name}” has another canonical ID than “{expected}”. It counts as 1 missing + 1 spurious relationship; fixing the entity fixes it.", {
            answer,
            name: endName(c, B),
            expected: endName(c, EXPECTED),
          }) + (ctx.whatIf ? ` ${t_i18n("The what-if counts it as a match.")}` : "")
        : "";
    case "type":
      return t_i18n("Both endpoints match, but {answer} used “{used}” where “{expected}” is expected. It counts as 1 missing + 1 spurious relationship.", {
        answer,
        used: r.kinds[B] ?? "",
        expected: r.kinds[EXPECTED] ?? "",
      });
    default:
      return "";
  }
}

function RelationshipInspector({ ctx, r }: { ctx: ReviewContext; r: RelationshipRow }) {
  const { t_i18n } = useFormatter();
  const it = ctx.derived.rels.get(r.id);
  const st = it ? displayStatus(it.st, ctx.mode, ctx.whatIf) : "same";
  const cause = it?.cause ? ctx.model.byId.get(it.cause) : undefined;
  const why = it ? whyRelationship(ctx, it, t_i18n) : "";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TypeChip>{t_i18n("relationship")}</TypeChip>
        <StatusPill status={st} label={it ? t_i18n(statusLabel(st, ctx.mode)) : t_i18n("Not in this comparison")} />
      </div>
      <h3 className="content-base-bold">{r.kinds[EXPECTED] ?? r.kinds[ctx.pair.under] ?? Object.values(r.kinds)[0]}</h3>
      <dl className="space-y-2">
        {ctx.shown.map((s) => (
          <div key={s}>
            <dt className="content-caption text-default-secondary">{sourceHead(ctx, s, t_i18n)}</dt>
            <dd className="content-compact break-words">
              {r.kinds[s] ? (
                <>
                  {s !== EXPECTED && <OkMark ctx={ctx} row={r} source={s} />}
                  {tripleText(r, s)}
                </>
              ) : (
                <span className="italic text-default-secondary">
                  {s === EXPECTED ? t_i18n("not expected") : r.kinds[EXPECTED] ? t_i18n("missed") : t_i18n("absent")}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      {why && <p className={cn("rounded-sm border-l-2 px-2 py-1 content-compact", STATUS_SURFACE[st])}>{why}</p>}
      {cause && (
        <p className="content-compact text-default-secondary">
          {t_i18n("Caused by")}{" "}
          <button
            type="button"
            onClick={() => ctx.select({ kind: "e", id: cause.id })}
            className="text-accent underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
          >
            {cause.type} · {endName(cause, EXPECTED)}
          </button>
        </p>
      )}
      <HistoryLine ctx={ctx} kind="r" id={r.id} />
    </div>
  );
}

export function ReviewInspector({ ctx }: { ctx: ReviewContext }) {
  const { t_i18n } = useFormatter();
  const sel = ctx.selection;
  const g = sel?.kind === "e" ? ctx.model.byId.get(sel.id) : undefined;
  const r = sel?.kind === "r" ? ctx.model.relById.get(sel.id) : undefined;
  return (
    <aside aria-label={t_i18n("Inspector")} aria-live="polite">
      {g ? (
        <EntityInspector ctx={ctx} g={g} />
      ) : r ? (
        <RelationshipInspector ctx={ctx} r={r} />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1">
            {[...new Set(ctx.review.answers.map((a) => a.evaluation_name))].map((name) => (
              <TypeChip key={name}>{name}</TypeChip>
            ))}
          </div>
          <h3 className="content-base-bold">{t_i18n("Pick an object")}</h3>
          <p className="content-compact text-default-secondary">
            {t_i18n("Select a row, a mention in the report, a node or an edge to see it in the expected bundle and in the compared answers side by side.")}
          </p>
          {ctx.review.entry.dataset_name && (
            <p className="content-compact text-default-secondary">
              {ctx.review.entry.dataset_description
                ? t_i18n("Dataset “{name}”: {description}", { name: ctx.review.entry.dataset_name, description: ctx.review.entry.dataset_description })
                : t_i18n("Dataset “{name}”", { name: ctx.review.entry.dataset_name })}
            </p>
          )}
        </div>
      )}
    </aside>
  );
}
