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

import { AlertTriangle, FlaskConical, MinusCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatDeviation, isControlArm, placeboWarningLabel, runArm, shortCorpusVersion } from "@/lib/placebo";
import type { EvaluationRunDetail, PlaceboSnapshot } from "@/types";
import { usePlaceboTargetLabel } from "./use-placebo-target-label";

/** A placebo part's name, read by `t_i18n(PART_LABELS[part])`. @i18n-keys */
const PART_LABELS = {
  listing: "Name and description",
  content: "Body",
} as const;

/**
 * What each fact of a placebo snapshot means, from FR-6 to FR-10 of
 * `dev-docs/agent-placebo-evaluation.md` and `services/evaluation_placebo.py`. @i18n-keys
 */
const SNAPSHOT_NOTES = {
  language:
    "The language detected in the original text; the placebo is drawn in that language. When it is undetermined, the placebo is in English.",
  unit: "The unit the lengths are matched in: the run model's tokens, or characters when the model could not be probed for a token count.",
  source: "Where the placebo text comes from: the neutral corpus shipped with the platform, or the placebo control's custom text.",
  corpusVersion: "The version of the neutral corpus the placebo was drawn from. Any change to the corpus makes a new version.",
  seed: "The seed that chose the corpus passages, derived from the comparison and the target, so a replay rebuilds the same placebo.",
  leakage:
    "Whether the placebo is free of the distinctive words of what it replaces, such as the agent's name or the replaced Prompts' and skills' names.",
  probes: "The model calls made to measure the placebo's length in tokens, and the input tokens they used.",
  deviation:
    "How far the placebo's length is from the original's, as a share of it. Beyond ±5 % the part is outside tolerance and marked with a warning sign.",
} as const;

/**
 * What a control arm ran with (FR-30): the target it replaced or removed and,
 * for a placebo arm, every placebo text - shown as plain text, never as
 * markdown or HTML - with its measured lengths and deviations, the corpus
 * version, the seed and the language.
 */
export function PlaceboSnapshotCard({ run }: { run: ControlRun }) {
  return isControlArm(run) ? <ControlArmCard run={run} /> : null;
}

type ControlRun = Pick<EvaluationRunDetail, "arm" | "placebo_target" | "placebo_snapshot">;

function ControlArmCard({ run }: { run: ControlRun }) {
  const { t_i18n } = useFormatter();
  const placebo = runArm(run) === "placebo";
  const target = usePlaceboTargetLabel(run.placebo_target);
  const snapshot = run.placebo_snapshot ?? null;

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-filigran-ia-primary/25">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base font-medium">
          {placebo ? <FlaskConical className="h-4 w-4 text-filigran-ia-primary" /> : <MinusCircle className="h-4 w-4 text-default-secondary" />}
          {placebo ? t_i18n("Placebo arm · {target}", { target }) : t_i18n("No-target arm · {target}", { target })}
        </CardTitle>
        <p className="content-compact text-default-secondary">
          {placebo
            ? t_i18n("This run replaced this target with the neutral text below, of the same length.")
            : t_i18n("This run removed this target; everything else is the real agent's.")}
        </p>
      </CardHeader>
      {placebo && (
        <CardContent className="space-y-4">
          {snapshot ? <SnapshotDetails snapshot={snapshot} /> : (
            <p className="content-compact text-default-secondary">{t_i18n("The placebo is built when the run starts.")}</p>
          )}
        </CardContent>
      )}
    </Card>
  );
}

function SnapshotDetails({ snapshot }: { snapshot: PlaceboSnapshot }) {
  const { t_i18n } = useFormatter();
  const unit = snapshot.unit === "tokens" ? t_i18n("tokens") : t_i18n("characters");
  // [label, value, shown as an identifier, what the label means]
  const facts: [string, string, boolean, string][] = [
    [t_i18n("Language"), snapshot.language_detected ? snapshot.language : t_i18n("{language} (undetermined)", { language: snapshot.language }), false, SNAPSHOT_NOTES.language],
    [t_i18n("Measured in"), unit, false, SNAPSHOT_NOTES.unit],
    [t_i18n("Source"), snapshot.custom_text ? t_i18n("Custom text") : t_i18n("Neutral corpus"), false, SNAPSHOT_NOTES.source],
    [t_i18n("Corpus version"), shortCorpusVersion(snapshot.corpus_version), true, SNAPSHOT_NOTES.corpusVersion],
    [t_i18n("Placebo seed"), snapshot.seed, true, SNAPSHOT_NOTES.seed],
    [t_i18n("Leakage check"), snapshot.leakage_check === "passed" ? t_i18n("Passed") : (snapshot.leakage_check ?? "-"), false, SNAPSHOT_NOTES.leakage],
  ];
  if (snapshot.probe_usage && snapshot.probe_usage.calls > 0) {
    facts.push([
      t_i18n("Token probes"),
      t_i18n("{calls, plural, one {# call} other {# calls}}, {tokens, number} input tokens", {
        calls: snapshot.probe_usage.calls,
        tokens: snapshot.probe_usage.input_tokens,
      }),
      false,
      SNAPSHOT_NOTES.probes,
    ]);
  }
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        {facts.map(([label, value, identifier, note]) => (
          <div key={label}>
            <p className="flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary">
              {label}
              <InfoTip content={t_i18n(note)} label={t_i18n("What {fact} means", { fact: label })} />
            </p>
            <p className={cn("break-all content-compact", identifier && "font-mono-plex")}>{value}</p>
          </div>
        ))}
      </div>
      {snapshot.warnings.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {snapshot.warnings.map((code) => {
            const label = placeboWarningLabel(code);
            return (
              <span key={code} className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] px-1.5 py-0.5 content-caption text-feedback-warning-tertiary">
                {label.known ? t_i18n(label.key) : label.key}
              </span>
            );
          })}
        </div>
      )}
      {snapshot.elements.map((element, index) => (
        <div key={index} className="rounded-sm border border-elevation-subtle-soft">
          {(element.name || element.description) && (
            <div className="border-b border-elevation-subtle-soft px-3 py-2">
              {element.name && <p className="whitespace-pre-wrap content-compact font-medium">{element.name}</p>}
              {element.description && <p className="whitespace-pre-wrap content-caption text-default-secondary">{element.description}</p>}
              {element.always_load && <p className="mt-0.5 content-caption text-accent">{t_i18n("Pinned: inlined in the system prompt")}</p>}
            </div>
          )}
          <p className="max-h-64 overflow-y-auto whitespace-pre-wrap px-3 py-2 content-compact text-default-secondary">{element.content}</p>
          <table className="w-full border-t border-elevation-subtle-soft content-caption">
            <thead>
              <tr className="text-default-secondary">
                <th className="px-3 py-1 text-left font-medium">{t_i18n("Part")}</th>
                <th className="px-3 py-1 text-right font-medium">{t_i18n("Original ({unit})", { unit })}</th>
                <th className="px-3 py-1 text-right font-medium">{t_i18n("Placebo ({unit})", { unit })}</th>
                <th className="px-3 py-1 text-right font-medium">
                  <InfoLabel content={t_i18n(SNAPSHOT_NOTES.deviation)}>{t_i18n("Deviation")}</InfoLabel>
                </th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(element.lengths).map(([part, length]) => (
                <tr key={part} className="border-t border-elevation-subtle-soft">
                  <td className="px-3 py-1">{part in PART_LABELS ? t_i18n(PART_LABELS[part as keyof typeof PART_LABELS]) : part}</td>
                  <td className="px-3 py-1 text-right font-mono-plex">{length.original}</td>
                  <td className="px-3 py-1 text-right font-mono-plex">{length.placebo}</td>
                  <td className={cn("px-3 py-1 text-right font-mono-plex", !length.within_tolerance && "text-feedback-warning-primary")}>
                    {formatDeviation(length.deviation)}
                    {/* Outside tolerance is said in words too, never by colour alone. */}
                    {!length.within_tolerance && (
                      <>
                        <AlertTriangle aria-hidden className="ml-1 inline h-3 w-3" />
                        <span className="sr-only"> {t_i18n("outside tolerance")}</span>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
