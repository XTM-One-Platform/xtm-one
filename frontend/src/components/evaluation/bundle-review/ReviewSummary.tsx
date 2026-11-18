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
import {
  EXPECTED,
  NONE,
  answerName,
  compareEntity,
  entityCounts,
  f1,
  formatScore,
  relationshipCounts,
  type DisplayStatus,
  type ReviewPair,
} from "@/lib/evaluation-bundle-review";
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
export function PairCounts({ ctx }: { ctx: ReviewContext }) {
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
  const shift = (official: number | null | undefined, sim: number, raw: number) => (official == null ? null : official + sim - raw);
  const answer = ctx.review.answers.find((a) => a.key === B);
  return (
    <div className="space-y-1">
      <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label={t_i18n("Objects by status")}>
        {counts.map(([st, value, label]) => (
          <Count key={label} status={st} value={value} label={label} />
        ))}
      </ul>
      <p className="content-caption text-default-secondary">
        {foot}
        {whatIf && mode !== "runs" && answer && (
          <>
            {" "}
            {t_i18n("With the what-if, {letter} would score Entity F1 {entity} and Relationship F1 {relationship}.", {
              letter: ctx.letter(B),
              entity: formatScore(shift(answer.metrics["stix.entity_prf"], f1(entityCounts(model, B, true)), f1(entityCounts(model, B, false)))),
              relationship: formatScore(
                shift(answer.metrics["stix.relationship_prf"], f1(relationshipCounts(model, B, true)), f1(relationshipCounts(model, B, false))),
              ),
            })}
          </>
        )}
      </p>
    </div>
  );
}
