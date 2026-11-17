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

import { useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Plus,
  Trash2,
  Wand2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { cn, formatShortDateTime } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { Tip } from "@/components/ui/tooltip";
import { InfoTip } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import {
  usePreviewExtractor,
  usePreviewExtractorForFamily,
  useExtractorSampleResults,
} from "@/hooks/api/useEvaluationApi";
import type {
  ExtractorOperation,
  ExtractorPreviewResult,
  ExtractorSpec,
} from "@/types";
import {
  emptyExtractorFor,
  EXTRACTOR_OPERATIONS,
  EXTRACTOR_OUTPUT_KINDS,
  EXTRACTOR_SOURCE_KINDS,
  EXTRACTOR_OPERATION_LABELS,
  EXTRACTOR_OUTPUT_KIND_TYPES,
  EXTRACTOR_SOURCE_LABELS,
  EXTRACTOR_VALUE_TYPE_LABELS,
  operationMeta,
} from "./extractor-catalog";
import { previewSample, sourceIsPreviewable } from "./extractor-preview";
import { extractionFailureLabel } from "@/lib/evaluation-glossary";
import { Spinner } from "@filigran/design-system";

/**
 * What the card's concepts mean, read from `services/evaluation/extraction/`:
 * `failures.py` (what a failed extraction does to a score), `engine.py` (each operation)
 * and `spec.py` (the output kinds a family scores). @i18n-keys
 */
const EXTRACTOR_NOTES = {
  card:
    "When extraction fails on an answer, the family's metrics score that entry 0, with the reason. When the extractor itself is at fault, the entry is left out of the scores instead.",
  source:
    "The text the extractor reads: the agent's answer, or the output or input of the agent's last successful call to the named tool.",
  operations:
    "The operations run in order, each on the previous one's result, starting from the source text. The last one must produce the output kind below.",
  output:
    "The shape the extraction must end with, one the output family scores: a label set for Classification, an object or an array for JSON.",
} as const;

/** What each operation does, as `engine.py` runs it. @i18n-keys */
const EXTRACTOR_OPERATION_DESCRIPTIONS = {
  find_json:
    "Finds the JSON in a text: the first code block holding JSON, else the outermost object or array. It does not repair invalid JSON.",
  parse_json: "Reads a text as JSON, which must be an object or an array.",
  select_path: "Takes the value at the path from an object, one key per dot segment. No index or wildcard.",
  object_keys: "Lists an object's keys.",
  object_values: "Lists an object's values.",
  collect_keys: "For each object of a list, takes the object at the path and adds its keys to one list.",
  flatten: "Turns a list of lists into one list, one level deep; an item that is not a list is kept as it is.",
  dedupe_sort: "Turns a list of non-empty texts into a label set: duplicates removed, sorted.",
} as const satisfies Record<ExtractorOperation["op"], string>;

/** Field heading at the design system Label size (body, 500), inside the wrapping label. */
const LABEL = "block content-base font-medium leading-none";

interface AgentRow {
  id: string;
  name: string;
}

/**
 * What a preview reads: a saved evaluation, whose stored results can be previewed
 * against too, or the family of an evaluation still being created, which has none yet and
 * previews a pasted sample.
 */
export type ExtractorPreviewTarget = { evaluationId: string } | { family: string };

interface Props {
  target: ExtractorPreviewTarget;
  /** The output kinds the evaluation's family scores (`GET /evaluations/families`), when known. */
  outputKinds?: ExtractorSpec["output_kind"][];
  agents: AgentRow[];
  extractors: Record<string, ExtractorSpec | null>;
  canManage: boolean;
  onChange: (next: Record<string, ExtractorSpec | null>) => void;
}

export function EvaluationExtractorCard({
  target,
  outputKinds,
  agents,
  extractors,
  canManage,
  onChange,
}: Props) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Wand2 className="h-3.5 w-3.5 text-default-secondary" />
          <CardTitle className="flex items-center gap-1 text-base font-medium">
            {t_i18n("Output extractors")}
            <InfoTip content={t_i18n(EXTRACTOR_NOTES.card)} label={t_i18n("What a failed extraction does to a score")} />
          </CardTitle>
        </div>
        <p className="content-base text-default-secondary">
          {t_i18n("Map an agent's raw output onto the shape this evaluation scores. An agent with no extractor is scored on its raw output.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {agents.length === 0 && (
          <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
            <Wand2 className="h-6 w-6 text-default-secondary mx-auto mb-2" />
            <p className="content-compact text-default-secondary">
              {"evaluationId" in target
                ? t_i18n("No agents on this evaluation yet. Add one to configure extraction.")
                : t_i18n("No agents selected yet. Add one to configure extraction.")}
            </p>
          </div>
        )}
        {agents.map((agent) => (
          <AgentExtractorRow
            key={agent.id}
            target={target}
            outputKinds={outputKinds}
            agent={agent}
            extractor={extractors[agent.id] ?? null}
            canManage={canManage}
            onChange={(next) => onChange({ [agent.id]: next })}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function AgentExtractorRow({
  target,
  outputKinds,
  agent,
  extractor,
  canManage,
  onChange,
}: {
  target: ExtractorPreviewTarget;
  outputKinds?: ExtractorSpec["output_kind"][];
  agent: AgentRow;
  extractor: ExtractorSpec | null;
  canManage: boolean;
  onChange: (next: ExtractorSpec | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const [sample, setSample] = useState("");
  const [previewSource, setPreviewSource] = useState<"sample" | "stored">("sample");
  const [resultId, setResultId] = useState("");
  const familyKey = `${"family" in target ? target.family : ""}|${(outputKinds ?? []).join(",")}`;
  const [previewState, setPreviewState] = useState<{ familyKey: string; result: ExtractorPreviewResult } | null>(null);
  // A verdict reached under another family no longer holds: the extractor is
  // checked against the family, so the author previews again.
  const preview = previewState?.familyKey === familyKey ? previewState.result : null;
  const setPreview = (result: ExtractorPreviewResult | null) =>
    setPreviewState(result ? { familyKey, result } : null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const previewMutation = usePreviewExtractor();
  const familyPreview = usePreviewExtractorForFamily();
  const previewPending = previewMutation.isPending || familyPreview.isPending;
  // A saved evaluation previews against its stored results too; one being created has
  // none, so it previews a pasted sample only, read from its family.
  const evaluationId = "evaluationId" in target ? target.evaluationId : "";
  const storedResults = useExtractorSampleResults(
    evaluationId,
    agent.id,
    extractor !== null && previewSource === "stored",
  );

  const enabled = extractor !== null;
  // The family's output kinds, in the catalog's order; the current one stays listed when
  // it does not fit, so the author sees it and the notice says why.
  const offeredKinds = outputKinds
    ? EXTRACTOR_OUTPUT_KINDS.filter((kind) => outputKinds.includes(kind) || kind === extractor?.output_kind)
    : EXTRACTOR_OUTPUT_KINDS;
  const familyTakesNoExtraction = outputKinds !== undefined && outputKinds.length === 0;
  const kindRefused =
    extractor !== null && outputKinds !== undefined && !outputKinds.includes(extractor.output_kind);

  const update = (patch: Partial<ExtractorSpec>) => {
    if (!extractor) return;
    onChange({ ...extractor, ...patch });
  };

  const setOperation = (index: number, next: ExtractorOperation) => {
    if (!extractor) return;
    const operations = [...extractor.operations];
    operations[index] = next;
    update({ operations });
  };

  // Operations fold in order, so a mis-ordered pair must be fixable without
  // retyping the rest.
  const moveOperation = (index: number, delta: number) => {
    if (!extractor) return;
    const target = index + delta;
    if (target < 0 || target >= extractor.operations.length) return;
    const operations = [...extractor.operations];
    [operations[index], operations[target]] = [operations[target], operations[index]];
    update({ operations });
  };

  const canRunPreview =
    previewSource === "sample"
      ? !!sample.trim() && extractor !== null && sourceIsPreviewable(extractor.source)
      : !!resultId;

  // A tool source previews the pasted text as that tool's last successful call.
  const toolName = extractor?.source.tool_name?.trim() ?? "";
  const samplePlaceholder =
    !extractor || extractor.source.kind === "agent_output"
      ? t_i18n("Paste a sample agent output")
      : !toolName
        ? t_i18n("Name the tool to preview this source.")
        : extractor.source.kind === "tool_output"
          ? t_i18n("Paste a sample output of {tool}", { tool: toolName })
          : t_i18n("Paste a sample input of {tool}", { tool: toolName });
  // The field's accessible name says what is asked for, as the placeholder does.
  const sampleLabel =
    extractor?.source.kind === "tool_input"
      ? t_i18n("Sample input to preview for {name}", { name: agent.name })
      : t_i18n("Sample output to preview for {name}", { name: agent.name });

  const runPreview = async () => {
    if (!extractor) return;
    setPreview(null);
    setRequestError(null);
    try {
      const result =
        "family" in target
          ? await familyPreview.mutateAsync({
              family: target.family,
              extractor,
              sample: previewSample(extractor.source, sample),
            })
          : await previewMutation.mutateAsync({
              evaluationId,
              agentId: agent.id,
              extractor,
              ...(previewSource === "stored"
                ? { resultId }
                : { sample: previewSample(extractor.source, sample) }),
            });
      setPreview(result);
    } catch (error) {
      // The request failed, not the extraction: said as such, never as a failure reason.
      setRequestError(error instanceof Error ? error.message : "");
    }
  };

  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-3">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="content-base font-medium truncate">{agent.name}</p>
          <p className="text-[11px] text-default-secondary mt-0.5">
            {enabled
              ? t_i18n("Extract before scoring")
              : familyTakesNoExtraction
                ? // Why the switch below is disabled.
                  t_i18n("This evaluation's family takes no output extraction in this version.")
                : t_i18n("Score the raw output")}
          </p>
        </div>
        <Switch
          checked={enabled}
          // A family with no extraction in this version can only switch one off.
          disabled={!canManage || (familyTakesNoExtraction && !enabled)}
          aria-label={t_i18n("Extract before scoring for {name}", { name: agent.name })}
          onCheckedChange={(checked) => onChange(checked ? emptyExtractorFor(outputKinds) : null)}
        />
      </div>

      {kindRefused && (
        <p className="flex items-start gap-2 content-compact text-feedback-warning-primary">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>
            {familyTakesNoExtraction
              ? t_i18n("This evaluation's family takes no output extraction in this version. Switch it off, or the evaluation cannot be saved.")
              : t_i18n("This evaluation's family does not score this output kind. Choose one it scores, or the evaluation cannot be saved.")}
          </span>
        </p>
      )}

      {extractor && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1">
              <span className={cn(LABEL, "flex items-center gap-1")}>
                {t_i18n("Source")}
                <InfoTip content={t_i18n(EXTRACTOR_NOTES.source)} label={t_i18n("What the extractor reads")} />
              </span>
              <Select
                value={extractor.source.kind}
                disabled={!canManage}
                onValueChange={(kind) =>
                  update({
                    source: {
                      kind: kind as ExtractorSpec["source"]["kind"],
                      tool_name:
                        kind === "agent_output" ? undefined : extractor.source.tool_name,
                    },
                  })
                }
              >
                <SelectTrigger className="w-full" aria-label={t_i18n("Source for {name}", { name: agent.name })}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXTRACTOR_SOURCE_KINDS.map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {t_i18n(EXTRACTOR_SOURCE_LABELS[kind])}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>

            {extractor.source.kind !== "agent_output" && (
              <label className="space-y-1.5">
                <span className={LABEL}>{t_i18n("Tool name")}</span>
                {/* `wrap` while disabled, so the reason still reaches hover and focus. */}
                <Tip content={t_i18n("The last successful call to this tool is used.")} wrap="auto" wrapClassName="flex w-full">
                  <Input
                    value={extractor.source.tool_name ?? ""}
                    disabled={!canManage}
                    aria-label={t_i18n("Tool name for {name}", { name: agent.name })}
                    onChange={(event) =>
                      update({
                        source: { ...extractor.source, tool_name: event.target.value },
                      })
                    }
                  />
                </Tip>
              </label>
            )}
          </div>

          <div className="space-y-2">
            <span className={cn(LABEL, "flex items-center gap-1")}>
              {t_i18n("Operations")}
              <InfoTip content={t_i18n(EXTRACTOR_NOTES.operations)} label={t_i18n("How the operations run")} />
            </span>
            <ol className="space-y-2">
              {extractor.operations.map((operation, index) => {
                const meta = operationMeta(operation.op);
                return (
                  <li key={index} className="flex flex-wrap items-center gap-2">
                    <span className="content-compact text-default-secondary w-6">{index + 1}.</span>
                    <Select
                      value={operation.op}
                      disabled={!canManage}
                      onValueChange={(op) =>
                        setOperation(index, {
                          op: op as ExtractorOperation["op"],
                          path: operationMeta(op as ExtractorOperation["op"])?.needsPath
                            ? operation.path ?? [""]
                            : undefined,
                        })
                      }
                    >
                      <SelectTrigger
                        className="h-9 w-64"
                        aria-label={t_i18n("Operation {n} for {name}", { n: index + 1, name: agent.name })}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {EXTRACTOR_OPERATIONS.map((entry) => (
                          <SelectItem key={entry.op} value={entry.op}>
                            {t_i18n(EXTRACTOR_OPERATION_LABELS[entry.op])}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {meta?.needsPath && (
                      <Input
                        className="h-9 w-56"
                        placeholder={t_i18n("path, e.g. predictions")}
                        value={(operation.path ?? []).join(".")}
                        disabled={!canManage}
                        aria-label={t_i18n("Path for operation {n} of {name}", { n: index + 1, name: agent.name })}
                        onChange={(event) =>
                          setOperation(index, {
                            ...operation,
                            path: event.target.value
                              .split(".")
                              .map((segment) => segment.trim())
                              .filter(Boolean),
                          })
                        }
                      />
                    )}
                    {meta && (
                      <span className="flex items-center gap-1 content-compact text-default-secondary">
                        {t_i18n(EXTRACTOR_VALUE_TYPE_LABELS[meta.accepts])} → {t_i18n(EXTRACTOR_VALUE_TYPE_LABELS[meta.produces])}
                        <InfoTip
                          content={t_i18n(EXTRACTOR_OPERATION_DESCRIPTIONS[operation.op])}
                          label={t_i18n("What operation {n} does", { n: index + 1 })}
                        />
                      </span>
                    )}
                    {canManage && (
                      <>
                        <Button
                          variant="ghost"
                          size="icon"
                          disabled={index === 0}
                          aria-label={t_i18n("Move operation {n} of {name} up", { n: index + 1, name: agent.name })}
                          onClick={() => moveOperation(index, -1)}
                        >
                          <ArrowUp className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          disabled={index === extractor.operations.length - 1}
                          aria-label={t_i18n("Move operation {n} of {name} down", { n: index + 1, name: agent.name })}
                          onClick={() => moveOperation(index, 1)}
                        >
                          <ArrowDown className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={t_i18n("Remove operation {n} of {name}", { n: index + 1, name: agent.name })}
                          onClick={() =>
                            update({
                              operations: extractor.operations.filter(
                                (_, i) => i !== index,
                              ),
                            })
                          }
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </>
                    )}
                  </li>
                );
              })}
            </ol>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  update({ operations: [...extractor.operations, { op: "flatten" }] })
                }
              >
                <Plus className="h-4 w-4 mr-1" /> {t_i18n("Add operation")}
              </Button>
            )}
          </div>

          <label className="space-y-1 block max-w-xs">
            <span className={cn(LABEL, "flex items-center gap-1")}>
              {t_i18n("Output")}
              <InfoTip content={t_i18n(EXTRACTOR_NOTES.output)} label={t_i18n("Which output kinds a family scores")} />
            </span>
            <Select
              value={extractor.output_kind}
              disabled={!canManage}
              onValueChange={(kind) =>
                update({ output_kind: kind as ExtractorSpec["output_kind"] })
              }
            >
              <SelectTrigger className="h-9" aria-label={t_i18n("Output kind for {name}", { name: agent.name })}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {offeredKinds.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {t_i18n(EXTRACTOR_VALUE_TYPE_LABELS[EXTRACTOR_OUTPUT_KIND_TYPES[kind]])}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>

          <div className="space-y-2 border-t border-elevation-subtle pt-4">
            <span className="block content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Preview")}</span>

            {"evaluationId" in target && (
              <label className="space-y-1.5 block max-w-xs">
                <span className={LABEL}>{t_i18n("Preview against")}</span>
                <Select
                  value={previewSource}
                  onValueChange={(next) => {
                    setPreviewSource(next as "sample" | "stored");
                    setPreview(null);
                    setRequestError(null);
                  }}
                >
                  <SelectTrigger className="h-9" aria-label={t_i18n("Preview source for {name}", { name: agent.name })}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="sample">{t_i18n("A pasted sample")}</SelectItem>
                    <SelectItem value="stored">{t_i18n("A stored result")}</SelectItem>
                  </SelectContent>
                </Select>
              </label>
            )}

            {previewSource === "stored" ? (
              <label className="space-y-1.5 block">
                <span className={LABEL}>{t_i18n("Stored result")}</span>
                <SearchableSelect
                  aria-label={t_i18n("Stored result to preview for {name}", { name: agent.name })}
                  options={(storedResults.data ?? []).map((row) => ({
                    value: row.result_id,
                    label: `${row.description ?? (row.position != null ? t_i18n("Entry {n}", { n: row.position + 1 }) : t_i18n("Entry"))} — ${formatShortDateTime(row.created_at)}`,
                  }))}
                  // The newest first, as the server lists them.
                  sort={false}
                  value={resultId}
                  onValueChange={setResultId}
                  placeholder={t_i18n("Select a stored result")}
                />
                {storedResults.data?.length === 0 && (
                  <span className="content-compact text-default-secondary">
                    {t_i18n("This agent has no stored results on this evaluation yet.")}
                  </span>
                )}
              </label>
            ) : (
              /* font-mono-plex: what is pasted here is a JSON payload, not prose. */
              <textarea
                className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary resize-y ${FIELD_FOCUS_RING}`}
                rows={4}
                placeholder={samplePlaceholder}
                value={sample}
                aria-label={sampleLabel}
                onChange={(event) => setSample(event.target.value)}
              />
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={runPreview}
              disabled={previewPending || !canRunPreview} aria-busy={previewPending || undefined}>
              {previewPending && <Spinner size="sm" tone="inherit" />}{!previewPending && <Wand2 className="h-4 w-4 mr-1" />}
              {previewPending ? t_i18n("Extracting…") : t_i18n("Preview")}
            </Button>

            {/* The outcome is announced as text, never by colour alone. */}
            <div aria-live="polite" className="min-h-[1.5rem] content-base">
              {previewPending && <span>{t_i18n("Extracting…")}</span>}
              {!previewPending && preview?.status === "extracted" && (
                <pre className="whitespace-pre-wrap break-all font-mono-plex content-compact">
                  {t_i18n("Extracted {count, plural, one {# item} other {# items}}", { count: preview.item_count ?? 0 })}: {preview.prediction}
                </pre>
              )}
              {!previewPending && requestError !== null && (
                <p className="flex items-start gap-2">
                  <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                  <span>{requestError ? `${t_i18n("Preview failed")}: ${requestError}` : t_i18n("Preview failed")}</span>
                </p>
              )}
              {!previewPending && preview?.status === "failed" && (
                <p className="flex items-start gap-2">
                  <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                  <span>
                    {t_i18n("Extraction failed")} — {extractionFailureLabel(preview.failure?.reason, t_i18n)}: {preview.failure?.message}
                  </span>
                </p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
