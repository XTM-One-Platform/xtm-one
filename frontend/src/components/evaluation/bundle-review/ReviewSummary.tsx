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

// The review's head: one card per answer with its official figures, the pair picker
// (which answer, against what) and the pair's object counts.

import { useId } from "react";
import { Link } from "react-router-dom";
import { Icon, IconButton } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import { Button } from "@/components/ui/button";
import {
  EXPECTED,
  NONE,
  answerName,
  compareEntity,
  entityCounts,
  f1,
  formatScore,
  relationshipCounts,
  typeCounts,
  type Counts,
  type DisplayStatus,
  type ReviewModel,
  type ReviewPair,
} from "@/lib/evaluation-bundle-review";
import { ENTITY_F1, ENTITY_F1_BY_TYPE, RELATIONSHIP_F1, precision, recall } from "@/lib/evaluation-run-review";
import { Delta, Tile } from "./figures";
import { cn, formatModelLabel } from "@/lib/utils";
import type { BundleReviewAnswer } from "@/types";
import { STATUS_DOT } from "./labels";
import type { ReviewContext } from "./context";

/** The metrics each answer card shows, read by `t_i18n`. @i18n-keys */
const METRIC_LABELS = {
  "stix.entity_prf": "Entity F1",
  "stix.relationship_prf": "Relationship F1",
} as const;

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="content-caption text-default-secondary">{label}</dt>
      <dd className="content-base-bold tabular-nums">{value}</dd>
    </div>
  );
}

function AnswerCard({ ctx, answer }: { ctx: ReviewContext; answer: BundleReviewAnswer }) {
  const { t_i18n } = useFormatter();
  const role = answer.key === ctx.pair.under ? t_i18n("Under review") : answer.key === ctx.pair.ref ? t_i18n("Reference") : null;
  const unreadable = ctx.review.unreadable.includes(answer.key);
  const name = answerName(ctx.review.answers, answer, (id) => formatModelLabel(id, t_i18n));
  return (
    <Card className={cn("layer-1 bg-elevation-default border-elevation-subtle-soft", answer.key === ctx.pair.under && "border-accent")}>
      <CardContent className="space-y-2 p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 content-compact font-semibold">
              <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-sm bg-elevation-highlight content-caption">{ctx.letter(answer.key)}</span>
              <span className="truncate">{name.title}</span>
            </p>
            <p className="truncate content-caption text-default-secondary">{name.detail}</p>
          </div>
          <Link
            to={`/evaluations/runs/${answer.run_id}`}
            aria-label={t_i18n("Open the run of answer {letter}", { letter: ctx.letter(answer.key) })}
            className="shrink-0 rounded-sm text-default-secondary hover:text-default-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
          >
            <Icon name="external-link" size={16} />
          </Link>
        </div>
        <div className="flex flex-wrap gap-1">
          {role && <Badge variant="outline">{role}</Badge>}
          {unreadable && <Badge variant="destructive">{t_i18n("Not a readable STIX bundle")}</Badge>}
          {answer.anomaly && <Badge variant="warning">{t_i18n("Anomaly")}</Badge>}
        </div>
        <dl className="grid grid-cols-3 gap-2">
          {(Object.keys(METRIC_LABELS) as (keyof typeof METRIC_LABELS)[]).map((k) => (
            <Figure key={k} label={t_i18n(METRIC_LABELS[k])} value={formatScore(answer.metrics[k])} />
          ))}
          <Figure label={t_i18n("Judge Score")} value={formatScore(answer.judge_score)} />
        </dl>
      </CardContent>
    </Card>
  );
}

export function AnswerCards({ ctx }: { ctx: ReviewContext }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {ctx.review.answers.map((a) => (
        <AnswerCard key={a.key} ctx={ctx} answer={a} />
      ))}
    </div>
  );
}

export function PairPicker({
  ctx,
  onPair,
  onWhatIf,
}: {
  ctx: ReviewContext;
  onPair: (pair: ReviewPair) => void;
  onWhatIf: (on: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const switchId = useId();
  const { answers } = ctx.review;
  const { ref, under } = ctx.pair;
  const pick = (next: ReviewPair) => onPair(next.ref === next.under ? { ref: EXPECTED, under: next.under } : next);
  return (
    <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
      {answers.length > 1 && (
        <div className="space-y-1">
          <p className="content-caption text-default-secondary">{t_i18n("Answer under review")}</p>
          <SegmentedFilter
            selection="radio"
            size="sm"
            ariaLabel={t_i18n("Answer under review")}
            value={under}
            onChange={(key) => pick({ ref, under: key })}
            options={answers.map((a) => ({ key: a.key, label: ctx.letter(a.key), tip: ctx.sourceName(a.key) }))}
          />
        </div>
      )}
      <div className="space-y-1">
        <p className="content-caption text-default-secondary">{t_i18n("Compared with")}</p>
        <SegmentedFilter
          selection="radio"
          size="sm"
          ariaLabel={t_i18n("Compared with")}
          value={ref}
          onChange={(key) => pick({ ref: key, under })}
          options={[
            { key: EXPECTED, label: "Expected bundle" },
            { key: NONE, label: "Nothing (answer alone)" },
            ...answers.filter((a) => a.key !== under).map((a) => ({ key: a.key, label: ctx.letter(a.key), tip: ctx.sourceName(a.key) })),
          ]}
        />
      </div>
      {ctx.mode === "runs" && (
        <IconButton
          size="sm"
          aria-label={t_i18n("Swap the two answers")}
          icon={<Icon name="arrow-right-left" size={16} />}
          onClick={() => onPair({ ref: under, under: ref })}
        />
      )}
      <label htmlFor={switchId} className="flex items-center gap-2 content-compact">
        <Switch id={switchId} checked={ctx.whatIf} onCheckedChange={onWhatIf} />
        {t_i18n("Count near-misses as matches (what-if)")}
      </label>
    </div>
  );
}

function Count({ status, value, label }: { status: DisplayStatus; value: number; label: string }) {
  return (
    <li className="flex items-center gap-1.5 content-compact">
      <span aria-hidden className={cn("h-2 w-2 rounded-full", STATUS_DOT[status])} />
      <span className="font-semibold tabular-nums">{value}</span>
      <span className="text-default-secondary">{label}</span>
    </li>
  );
}

/** What the pair says in numbers: the objects per status, and the what-if's figures. */
function PairCounts({ ctx }: { ctx: ReviewContext }) {
  const { t_i18n } = useFormatter();
  const { model, mode, pair, whatIf, derived } = ctx;
  const B = pair.under;
  const n = (st: string) => derived.entItems.filter((it) => it.st === st).length;
  const c = entityCounts(model, B, whatIf);
  const nNear = model.groups.filter((g) => compareEntity(g, EXPECTED, B) === "near").length;
  const counts: [DisplayStatus, number, string][] =
    mode === "runs"
      ? [
          ["fixed", n("fixed"), t_i18n("fixed")],
          ["regressed", n("regressed"), t_i18n("regressed")],
          ["new", n("new"), t_i18n("new errors")],
          ["still", n("still"), t_i18n("still wrong")],
        ]
      : mode === "alone"
        ? [
            ["tp", c.tp, t_i18n("correct")],
            ["fp", whatIf ? c.fp : n("fp"), t_i18n("not expected")],
            ["near", whatIf ? 0 : n("near"), t_i18n("near-misses")],
            ["fn", model.groups.filter((g) => g.by[EXPECTED] && !g.by[B]).length, t_i18n("expected, not found")],
          ]
        : [
            ["tp", c.tp, t_i18n("matched")],
            ["fn", whatIf ? c.fn : n("fn"), t_i18n("missing")],
            ["fp", whatIf ? c.fp : n("fp"), t_i18n("spurious")],
            ["near", whatIf ? 0 : n("near"), t_i18n("near-miss pairs")],
          ];
  const foot =
    mode === "runs"
      ? t_i18n("Objects compared between {a} and {b}, each judged against the expected bundle.", { a: ctx.letter(pair.ref), b: ctx.letter(B) })
      : whatIf
        ? t_i18n("{count, plural, one {# near-miss counted as a match} other {# near-misses counted as matches}}.", { count: nNear })
        : t_i18n("{count, plural, one {# near-miss counts as 1 missing + 1 spurious} other {# near-misses count as # missing + # spurious}}.", { count: nNear });
  return (
    <div className="space-y-1">
      <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label={t_i18n("Objects by status")}>
        {counts.map(([st, value, label]) => (
          <Count key={label} status={st} value={value} label={label} />
        ))}
      </ul>
      <p className="content-caption text-default-secondary">{foot}</p>
    </div>
  );
}

/** The mean of each object type's Entity F1 (types the answer or the expected bundle hold). */
function macroF1(model: ReviewModel, s: string, whatIf: boolean): number {
  const v = model.types.map((t) => typeCounts(model, s, whatIf, t)).filter((c) => c.tp + c.fp + c.fn > 0).map(f1);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 100;
}

/**
 * The scores on this report: Entity F1, Relationship F1, Entity F1 per type and the judge
 * score of the answer under review, each with its change against the comparator, then its
 * objects by status. The official figure is the scorer's; the what-if adds the change the
 * page computes, and the official one stays, struck through.
 */
export function ScoreCards({ ctx, onOpenJudge }: { ctx: ReviewContext; onOpenJudge: () => void }) {
  const { t_i18n } = useFormatter();
  const { fullModel, mode, pair, whatIf, review } = ctx;
  const B = pair.under;
  const answerOf = (s: string | null | undefined) => (s ? review.answers.find((a) => a.key === s) : undefined);
  const answer = answerOf(B);
  const readable = (s: string) => !review.unreadable.includes(s) && !!answerOf(s);
  const prev = ctx.comparator && readable(ctx.comparator.key) ? ctx.comparator : null;
  const bOk = readable(B);
  const card = (
    label: string,
    official: (s: string) => number | null | undefined,
    js: (s: string, sim: boolean) => number,
    sub: (v: number | null, pv: number | null) => string,
  ) => {
    if (!bOk) return <Tile key={label} label={label} value="—" sub={t_i18n("No readable answer")} />;
    const val = (s: string, sim: boolean) => {
      const o = official(s);
      return o == null ? null : o + (sim ? js(s, true) - js(s, false) : 0);
    };
    const v = val(B, whatIf);
    const raw = val(B, false);
    const pv = prev ? val(prev.key, whatIf) : null;
    const shifted = whatIf && v != null && raw != null && Math.abs(v - raw) > 0.05;
    return (
      <Tile
        key={label}
        label={label}
        value={formatScore(v)}
        was={shifted ? formatScore(raw) : null}
        highlight={shifted}
        delta={prev ? <Delta now={v} was={pv} vs={prev.name} /> : null}
        sub={sub(v, pv)}
      />
    );
  };
  const official = (key: string, s: string): number | null | undefined => answerOf(s)?.metrics[key];
  const prf = (key: string, js: Counts) => {
    // The scorer's own counts when the what-if is off; the page's counts with it.
    const served = answer?.counts?.[key];
    const c = !whatIf && served ? { tp: served[0], fp: served[1], fn: served[2] } : js;
    return t_i18n("P {precision} · R {recall}", { precision: formatScore(precision(c)), recall: formatScore(recall(c)) });
  };
  const sub = (key: string, js: Counts) => (v: number | null, pv: number | null) =>
    mode === "runs"
      ? `${ctx.letter(pair.ref)} ${formatScore(pv)} → ${ctx.letter(B)} ${formatScore(v)}`
      : prf(key, js) + (prev && pv != null ? ` · ${prev.name} ${formatScore(pv)}` : "");
  const types = fullModel.types.filter((t) => {
    const c = typeCounts(fullModel, B, whatIf, t);
    return c.tp + c.fp + c.fn > 0;
  }).length;
  const judge = answer?.judge_score ?? null;
  const prevAnswer = answerOf(prev?.key);
  const prevJudge = prevAnswer && prevAnswer.evaluation_id === answer?.evaluation_id ? prevAnswer.judge_score : null;
  return (
    <section aria-label={t_i18n("Scores on this report")} className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
      {card(
        t_i18n("Entity F1"),
        (s) => official(ENTITY_F1, s),
        (s, x) => f1(entityCounts(fullModel, s, x)),
        sub(ENTITY_F1, entityCounts(fullModel, B, whatIf)),
      )}
      {card(
        t_i18n("Relationship F1"),
        (s) => official(RELATIONSHIP_F1, s),
        (s, x) => f1(relationshipCounts(fullModel, s, x)),
        sub(RELATIONSHIP_F1, relationshipCounts(fullModel, B, whatIf)),
      )}
      {card(
        t_i18n("Entity F1 per type"),
        // Before the scorer served the per-type figure, the page's own reading stands in.
        (s) => official(ENTITY_F1_BY_TYPE, s) ?? macroF1(fullModel, s, false),
        (s, x) => macroF1(fullModel, s, x),
        (v, pv) =>
          mode === "runs"
            ? `${ctx.letter(pair.ref)} ${formatScore(pv)} → ${ctx.letter(B)} ${formatScore(v)}`
            : t_i18n("{count, plural, =0 {no type} one {# type} other {# types}}", { count: types }) + (prev && pv != null ? ` · ${prev.name} ${formatScore(pv)}` : ""),
      )}
      <Tile
        label={t_i18n("Judge score")}
        value={bOk ? formatScore(judge) : "—"}
        delta={bOk && prev && prevJudge != null ? <Delta now={judge} was={prevJudge} vs={prev.name} /> : null}
        sub={
          answer?.judge_reasoning ? (
            <Button variant="link" size="sm" className="h-auto p-0" onClick={onOpenJudge}>
              {t_i18n("Read the judge’s reasoning")}
            </Button>
          ) : judge != null ? (
            t_i18n("LLM judge, weighted sub-scores")
          ) : (
            t_i18n("No judge verdict on this answer")
          )
        }
      />
      <Tile label={mode === "runs" ? t_i18n("Objects · A → B") : t_i18n("Objects")}>
        <PairCounts ctx={ctx} />
      </Tile>
    </section>
  );
}

/** The judge's reasoning on the answer under review, opened from its score card. */
export function JudgePanel({ ctx, onClose }: { ctx: ReviewContext; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const answer = ctx.review.answers.find((a) => a.key === ctx.pair.under);
  if (!answer?.judge_reasoning) return null;
  return (
    <section aria-label={t_i18n("What the judge said")} className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-default p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="content-compact">
          <span className="font-semibold">{t_i18n("What the judge said")}</span>
          <span className="text-default-secondary">
            {" "}
            · {ctx.sourceName(answer.key)} · {ctx.review.entry.label}
          </span>
        </p>
        <Button variant="link" size="sm" className="h-auto p-0" onClick={onClose}>
          {t_i18n("Close")}
        </Button>
      </div>
      <p className="whitespace-pre-wrap content-compact">{answer.judge_reasoning}</p>
    </section>
  );
}
