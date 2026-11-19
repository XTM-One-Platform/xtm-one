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

// Coverage: every object and relationship of the expected bundle and of every answer, one
// row each, one column per source: ✓ found, ≈ found in another form, — missed, ✗ not expected.

import type { ReactNode } from "react";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  compareEntity,
  compareRelationship,
  f1,
  formatScore,
  nearKind,
  relationshipCounts,
  tripleText,
  typeCounts,
  type ObjectGroup,
  type RelationshipRow,
} from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { NEAR_KIND_LABELS, STATUS_TEXT } from "./labels";
import { isSelected, type ReviewContext } from "./context";

function Cell({ mark, tone, text, note }: { mark: string; tone: string; text: string; note?: string }) {
  return (
    <td className="px-2 py-1 align-top">
      <span className="flex gap-1">
        <span aria-hidden className={cn("font-semibold", tone)}>{mark}</span>
        <span className="min-w-0 break-words">
          {text}
          {note && <span className="block content-caption text-default-secondary">{note}</span>}
        </span>
      </span>
    </td>
  );
}

function EntityCell({ ctx, g, s }: { ctx: ReviewContext; g: ObjectGroup; s: string }) {
  const { t_i18n } = useFormatter();
  const o = g.by[s];
  if (!o) {
    return g.by[EXPECTED] ? <Cell mark="–" tone={STATUS_TEXT.fn} text={t_i18n("missed")} /> : <td />;
  }
  const st = compareEntity(g, EXPECTED, s);
  const text = o.name + (o.type !== g.type ? ` (${o.type})` : "");
  const extra = g.extra[s] ? t_i18n("+{count, plural, one {# duplicate} other {# duplicates}}", { count: g.extra[s] }) : undefined;
  if (st === "tp" || (ctx.whatIf && st === "near")) return <Cell mark="✓" tone={STATUS_TEXT.tp} text={text} note={extra} />;
  if (st === "near") {
    const kind = t_i18n(NEAR_KIND_LABELS[nearKind(g.by[EXPECTED], o)]);
    return <Cell mark="≈" tone={STATUS_TEXT.near} text={text} note={extra ? `${kind} · ${extra}` : kind} />;
  }
  return <Cell mark="✗" tone={STATUS_TEXT.fp} text={text} note={extra ? `${t_i18n("not expected")} · ${extra}` : t_i18n("not expected")} />;
}

function RelationshipCell({ ctx, r, s }: { ctx: ReviewContext; r: RelationshipRow; s: string }) {
  const { t_i18n } = useFormatter();
  const text = tripleText(r, s);
  if (!text) return r.kinds[EXPECTED] ? <Cell mark="–" tone={STATUS_TEXT.fn} text={t_i18n("missed")} /> : <td />;
  const st = compareRelationship(ctx.model, r, EXPECTED, s)?.st;
  if (st === "tp" || (ctx.whatIf && st === "cascade")) return <Cell mark="✓" tone={STATUS_TEXT.tp} text={text} />;
  if (st === "cascade") return <Cell mark="≈" tone={STATUS_TEXT.near} text={text} note={t_i18n("endpoint named differently")} />;
  if (st === "type") return <Cell mark="≈" tone={STATUS_TEXT.near} text={text} note={t_i18n("type {type} expected", { type: r.kinds[EXPECTED] ?? "" })} />;
  return <Cell mark="✗" tone={STATUS_TEXT.fp} text={text} note={t_i18n("not expected")} />;
}

/** One row; its first cell is the button that selects it, so a keyboard reaches every row. */
function Row({ selected, onSelect, first, muted, children }: { selected: boolean; onSelect: () => void; first: string; muted: boolean; children: ReactNode }) {
  return (
    <tr onClick={onSelect} className={cn("cursor-pointer border-t border-elevation-subtle hover:bg-elevation-hover", selected && "bg-elevation-highlight")}>
      <td className="px-2 py-1 align-top">
        <button
          type="button"
          aria-pressed={selected}
          onClick={(e) => {
            e.stopPropagation();
            onSelect();
          }}
          className={cn(
            "w-full text-left break-words rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
            muted && "italic text-default-secondary",
          )}
        >
          {first}
        </button>
      </td>
      {children}
    </tr>
  );
}

export function CoverageView({ ctx }: { ctx: ReviewContext }) {
  const { t_i18n } = useFormatter();
  const { model, filters } = ctx;
  const answers = ctx.columns;
  const cols = [EXPECTED, ...answers];
  const shown = (g: ObjectGroup) => cols.some((s) => g.by[s]);
  const rels = model.rels
    .filter((r) => cols.some((s) => r.kinds[s]) && (!filters.type || model.byId.get(r.src)?.type === filters.type || model.byId.get(r.tgt)?.type === filters.type))
    .sort((x, y) => Number(!!y.kinds[EXPECTED]) - Number(!!x.kinds[EXPECTED]));
  const expected = model.groups.filter((g) => g.by[EXPECTED]);
  return (
    <div className="space-y-2">
      <p className="content-compact text-default-secondary">
        {t_i18n("Every object of the expected bundle and of the answers, one row each: ✓ found, ≈ found in another form, – missed, ✗ not expected.")}
      </p>
      <div className="overflow-x-auto rounded-sm border border-elevation-subtle">
        <table className="w-full content-compact">
          <thead className="bg-elevation-hover">
            <tr>
              {cols.map((s) => {
                const answer = ctx.review.answers.find((a) => a.key === s);
                return (
                  <th key={s} scope="col" className="px-2 py-1.5 text-left align-top font-medium">
                    {s === EXPECTED ? t_i18n("Expected") : `${ctx.letter(s)} · ${ctx.sourceName(s)}`}
                    <span className="block content-caption font-normal text-default-secondary">
                      {s === EXPECTED
                        ? t_i18n("{objects} objects · {relationships} relationships", {
                            objects: expected.length,
                            relationships: model.rels.filter((r) => r.kinds[EXPECTED]).length,
                          })
                        : ctx.review.unreadable.includes(s)
                          ? t_i18n("Not a readable STIX bundle")
                          : t_i18n("Entity F1 {entity} · Relationship F1 {relationship}", {
                              entity: formatScore(answer?.metrics["stix.entity_prf"]),
                              relationship: formatScore(answer?.metrics["stix.relationship_prf"]),
                            })}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {model.types.map((type) => {
              if (filters.type && filters.type !== type) return null;
              const gs = model.groups.filter((g) => g.type === type && shown(g)).sort((x, y) => Number(!!y.by[EXPECTED]) - Number(!!x.by[EXPECTED]));
              if (!gs.length) return null;
              return [
                <tr key={`h-${type}`} className="border-t border-elevation-subtle bg-elevation-hover">
                  <th scope="rowgroup" className="px-2 py-1 text-left font-mono-plex font-medium">{type}</th>
                  {answers.map((s) => (
                    <td key={s} className="px-2 py-1 tabular-nums text-default-secondary">
                      {t_i18n("F1 {value}", { value: formatScore(f1(typeCounts(model, s, ctx.whatIf, type))) })}
                    </td>
                  ))}
                </tr>,
                ...gs.map((g) => (
                  <Row
                    key={g.id}
                    selected={isSelected(ctx, "e", g.id)}
                    onSelect={() => ctx.select({ kind: "e", id: g.id })}
                    first={g.by[EXPECTED] ? g.by[EXPECTED].name : t_i18n("not expected")}
                    muted={!g.by[EXPECTED]}
                  >
                    {answers.map((s) => <EntityCell key={s} ctx={ctx} g={g} s={s} />)}
                  </Row>
                )),
              ];
            })}
            {rels.length > 0 && (
              <tr className="border-t border-elevation-subtle bg-elevation-hover">
                <th scope="rowgroup" className="px-2 py-1 text-left font-medium">{t_i18n("relationships")}</th>
                {answers.map((s) => (
                  <td key={s} className="px-2 py-1 tabular-nums text-default-secondary">
                    {t_i18n("F1 {value}", { value: formatScore(f1(relationshipCounts(model, s, ctx.whatIf))) })}
                  </td>
                ))}
              </tr>
            )}
            {rels.map((r) => (
              <Row
                key={r.id}
                selected={isSelected(ctx, "r", r.id)}
                onSelect={() => ctx.select({ kind: "r", id: r.id })}
                first={tripleText(r, EXPECTED) ?? t_i18n("not expected")}
                muted={!r.kinds[EXPECTED]}
              >
                {answers.map((s) => <RelationshipCell key={s} ctx={ctx} r={r} s={s} />)}
              </Row>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-elevation-subtle bg-elevation-hover content-caption">
              <td className="px-2 py-1">{t_i18n("{count, plural, one {# expected object} other {# expected objects}}", { count: expected.length })}</td>
              {answers.map((s) => {
                const found = expected.filter((g) => {
                  const st = compareEntity(g, EXPECTED, s);
                  return st === "tp" || (ctx.whatIf && st === "near");
                }).length;
                const near = ctx.whatIf ? 0 : expected.filter((g) => compareEntity(g, EXPECTED, s) === "near").length;
                return (
                  <td key={s} className="px-2 py-1">
                    {near
                      ? t_i18n("{found} of {total} found · {near} in another form · {missed} missed · {extra} not expected", {
                          found,
                          total: expected.length,
                          near,
                          missed: expected.filter((g) => !g.by[s]).length,
                          extra: model.groups.filter((g) => g.by[s] && !g.by[EXPECTED]).length,
                        })
                      : t_i18n("{found} of {total} found · {missed} missed · {extra} not expected", {
                          found,
                          total: expected.length,
                          missed: expected.filter((g) => !g.by[s]).length,
                          extra: model.groups.filter((g) => g.by[s] && !g.by[EXPECTED]).length,
                        })}
                  </td>
                );
              })}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
