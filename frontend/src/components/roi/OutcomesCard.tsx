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
 * "Outcomes" card (#9536, slice O6 of #9206; design dev-docs/roi-outcomes.md
 * 6.1): the unit of work an agent, an assignment or a fleet declares and how
 * its work is counted in ROI. One card for the three subjects, both modes,
 * any agent or fleet. Two states, never together: inherited (read-only, an
 * outline "Customize") or declared here (editable, "Reset to inherited"). A
 * built-in agent's shipped declaration reads as declared here with the Origin
 * "Default" (#9836 R3b): customizable, nothing to reset.
 * Reading and saving starts no run and calls no model.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Pencil, Target } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EditionBadge } from "@/components/ui/enterprise-upsell";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { useRoiUnits } from "@/hooks/api/useRoiCatalogApi";
import { RoiDefaultDiff } from "./RoiDefaultDiff";
import { originOf, useCustomizedLine } from "./roi-origin";
import { RoiOriginBadge } from "./RoiOriginBadge";
import { RoiSuggestionRow } from "./RoiSuggestionRow";
import {
  useDeleteRoiDeclaration,
  usePutRoiDeclaration,
  useRoiDeclaration,
  useRoiSuggestion,
  type RoiDeclaration,
  type RoiSubject,
} from "@/hooks/api/useRoiDeclarationApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

import {
  COUNT_MODE_MEANINGS,
  countModeOptions,
  draftOf,
  inputOf,
  isDraftError,
  REFUSAL_MESSAGES,
  saveRefusalOf,
  suggestionDeclaration,
  unitOptions,
  type DeclarationDraft,
  type DraftError,
} from "./roi-outcomes";

/** Settings > Organization > Units of work. */
const UNITS_SETTINGS_PATH = "/settings/organization/units";

/** Where an inherited declaration comes from, nearest first (assignment: its agent, then the fleet). */
export interface OutcomesParent {
  subject: RoiSubject;
  /** "fleet" or "agent": the line reads "Inherited from {kind} {name}". */
  kind: "fleet" | "agent";
  name: string;
}

export function OutcomesCard({
  subject,
  parents = [],
  structuredOutput,
  canManage,
  testId = "roi-outcomes-card",
  onEditingChange,
  chrome = "card",
  pickOnOpen = false,
}: {
  subject: RoiSubject;
  parents?: readonly OutcomesParent[];
  structuredOutput: boolean;
  canManage: boolean;
  testId?: string;
  /** Told when the card enters or leaves edit mode: a host form with its own Save guards the unsaved draft. */
  onEditingChange?: (editing: boolean) => void;
  /**
   * "card" (default): a Card of its own, beside the other cards of a page.
   * "section": the section chrome of a settings panel (a top rule and a 12 px
   * heading, like its "Run defaults" section), never a Card nested in a panel.
   */
  chrome?: "card" | "section";
  /**
   * Opened from a "Pick a unit" action elsewhere (the agent Overview's ROI, #9810): when
   * nothing is declared nor inherited and the viewer may edit, the card opens on the unit
   * picker, focused, once.
   */
  pickOnOpen?: boolean;
}) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const units = useRoiUnits();
  const own = useRoiDeclaration(subject);
  // At most two parents (an assignment's agent, then the fleet): fixed hook calls.
  const first = useRoiDeclaration(parents[0]?.subject ?? null);
  const second = useRoiDeclaration(parents[1]?.subject ?? null);
  const put = usePutRoiDeclaration(subject);
  const remove = useDeleteRoiDeclaration(subject);
  const [confirmDialog, confirm] = useConfirm();
  const customizedLine = useCustomizedLine();

  // A parent the viewer cannot read (403) counts as declaring nothing.
  const inherited = useMemo<{ parent: OutcomesParent; declaration: RoiDeclaration } | null>(() => {
    if (parents[0] && first.data) return { parent: parents[0], declaration: first.data };
    if (parents[1] && second.data) return { parent: parents[1], declaration: second.data };
    return null;
  }, [parents, first.data, second.data]);

  // Anyone who can see the subject reads its declaration, shown read-only with its Origin
  // (#9836 R3b, UX round 2 B1). An older API answered 403 to a non-manager: the card is then
  // read-only and says who declares a unit, never a read error (UX #9810 r3 B1).
  const forbidden = (own.error as { status?: number } | null)?.status === 403;
  const editable = canManage && !forbidden;
  const declared = own.data ?? null;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<DeclarationDraft>(() => draftOf(null, structuredOutput));
  const [fieldError, setFieldError] = useState<DraftError | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) setDraft(draftOf(declared, structuredOutput));
  }, [declared, editing, structuredOutput]);

  useEffect(() => {
    onEditingChange?.(editing);
  }, [editing, onEditingChange]);

  const options = useMemo(
    () => unitOptions(units.data ?? [], { isXtmOne, translate: t_i18n, locale }),
    [units.data, isXtmOne, t_i18n, locale],
  );
  const unitName = (key: string) => units.data?.find((u) => u.key === key)?.name ?? key;
  const archived = (key: string) => !!units.data?.find((u) => u.key === key)?.archived_at;

  const describeCount = (d: Pick<RoiDeclaration, "count_mode" | "count_path" | "units_per_count">) =>
    d.count_mode === "per_run"
      ? t_i18n("Per run, {count, plural, one {# unit} other {# units}} per run", { count: d.units_per_count })
      : d.count_mode === "per_output_item"
        ? t_i18n("Per output item at {path}", { path: d.count_path ?? "" })
        : t_i18n("Per work item completed");

  const startEditing = (from: RoiDeclaration | null) => {
    setDraft(draftOf(from, structuredOutput));
    setFieldError(null);
    setSaveError(null);
    setEditing(true);
  };

  const save = () => {
    const body = inputOf(draft);
    if (isDraftError(body)) {
      setFieldError(body);
      document.getElementById(`${testId}-${body.field}`)?.focus();
      return;
    }
    setFieldError(null);
    setSaveError(null);
    put.mutate(body, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: t_i18n("Outcomes saved"), variant: "success" });
      },
      onError: (err: Error) => {
        // Never the backend text: the field it names with a translated message, else one sentence.
        const refusal = saveRefusalOf(err);
        if (refusal.field === "unit") void units.refetch();
        if (refusal.field) {
          setFieldError({ field: refusal.field, message: refusal.message });
          document.getElementById(`${testId}-${refusal.field}`)?.focus();
        } else {
          setSaveError(t_i18n(refusal.message));
        }
      },
    });
  };

  // A built-in agent's customized declaration resets to the one it ships with (#9836 R3b).
  const builtinDefault = declared && originOf(declared) === "customized" ? (declared.default ?? null) : null;
  // The declaration a built-in agent ships with, answered with the Origin "Default" when no
  // one customized it (UX round 1 B2): nothing to reset, "Customize" edits it.
  const shipped = declared?.origin === "default";

  const resetToInherited = async () => {
    const ok = await confirm({
      title: builtinDefault ? t_i18n("Reset to default?") : parents.length > 0 ? t_i18n("Reset to inherited?") : t_i18n("Stop counting in ROI?"),
      description: builtinDefault
        ? t_i18n("This declaration is removed and the one this agent ships with applies again.")
        : inherited
          ? t_i18n("This declaration is removed and the one of {name} applies again.", { name: inherited.parent.name })
          : t_i18n("This declaration is removed: the work is no longer counted in ROI."),
      confirmLabel: t_i18n("Reset"),
      variant: "warning",
    });
    if (!ok) return;
    setSaveError(null);
    remove.mutate(undefined, {
      onSuccess: () => setEditing(false),
      onError: () => setSaveError(t_i18n(REFUSAL_MESSAGES.reset)),
    });
  };

  const loading = own.isLoading || units.isLoading;
  const readError = (own.isError && !forbidden) || units.isError;

  // A custom agent that counts nothing gets a suggested unit, accepted in one click (#9836 R8b).
  // Read only for an editor, once nothing is declared nor inherited: a built-in agent answers
  // `declared` and no suggestion.
  const parentsSettled = !first.isLoading && !second.isLoading;
  const wantsSuggestion =
    subject.type === "agent" && editable && !loading && !readError && !declared && !inherited && parentsSettled;
  const suggest = useRoiSuggestion(wantsSuggestion ? { agentId: subject.id } : null);
  const suggestion = wantsSuggestion && suggest.data && !suggest.data.declared ? suggest.data.suggestion : null;

  const acceptSuggestion = () => {
    if (!suggestion) return;
    setSaveError(null);
    put.mutate(suggestionDeclaration(suggestion), {
      onSuccess: () => {
        // The suggestion row unmounts once the declaration reads back: keep keyboard focus on
        // the card by moving it to the declared card's Edit control (UX r1 B2, #10245).
        focusEditAfterAccept.current = true;
        toast({ title: t_i18n("Outcomes saved"), variant: "success" });
      },
      onError: () => setSaveError(t_i18n("The suggested unit could not be saved. Pick a unit instead.")),
    });
  };
  const focusEditAfterAccept = useRef(false);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!focusEditAfterAccept.current || !declared || editing) return;
    focusEditAfterAccept.current = false;
    editButtonRef.current?.focus();
  }, [declared, editing]);
  const fieldId = (field: DraftError["field"]) => `${testId}-${field}`;

  const picked = useRef(false);
  useEffect(() => {
    if (!pickOnOpen || picked.current || !editable || loading || readError || declared || inherited) return;
    if (first.isLoading || second.isLoading) return;
    picked.current = true;
    startEditing(null);
  });
  useEffect(() => {
    if (editing && picked.current) document.getElementById(fieldId("unit"))?.focus();
    // Focus once, when the picker opens from pickOnOpen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);
  const errorOf = (field: DraftError["field"]) => (fieldError?.field === field ? t_i18n(fieldError.message) : undefined);
  const countLabelId = `${testId}-count-label`;
  const countHelpId = `${testId}-count-help`;

  const readOnlyRows = (d: RoiDeclaration) => (
    <dl className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-4 gap-y-2 content-base">
      <dt className="text-default-secondary">{t_i18n("Unit of work")}</dt>
      <dd className="text-default-primary">
        {unitName(d.unit)}
        {archived(d.unit) && <span className="ml-2 content-caption text-default-secondary">{t_i18n("Archived")}</span>}
      </dd>
      <dt className="text-default-secondary">{t_i18n("Counting mode")}</dt>
      <dd className="text-default-primary">{describeCount(d)}</dd>
    </dl>
  );

  const body = (
    <>
        {loading ? (
          <Skeleton className="h-16 w-full" />
        ) : readError ? (
          <p role="alert" className="content-compact text-feedback-error-primary">
            {t_i18n("The outcomes declaration could not be read.")}
          </p>
        ) : editing ? (
          <div className="space-y-4">
            <SettingsField
              id={fieldId("unit")}
              label={t_i18n("Unit of work")}
              required
              error={errorOf("unit")}
              info={
                isXtmOne
                  ? t_i18n(
                      "One piece of work a person would do by hand, with the minutes it takes them: ROI multiplies the units counted by those minutes. Example: CVE assessed counts each CVE this work assesses. Empty: the work is not counted in ROI.",
                    )
                  : t_i18n(
                      "One piece of work a person would do by hand, with the minutes it takes them: ROI multiplies the units counted by those minutes. Example: Support ticket resolved counts each ticket this work resolves. Empty: the work is not counted in ROI.",
                    )
              }
              helper={
                <span className="flex flex-wrap items-center gap-x-2">
                  {isXtmOne
                    ? t_i18n("What one piece of this work is, for example one threat report triaged.")
                    : t_i18n("What one piece of this work is, for example one email triaged and answered.")}
                  {isAdmin && (
                    <Link to={UNITS_SETTINGS_PATH} className="text-accent hover:underline">
                      {t_i18n("Manage units")}
                    </Link>
                  )}
                </span>
              }
            >
              <SearchableSelect
                id={fieldId("unit")}
                aria-label={t_i18n("Unit of work")}
                aria-describedby={settingsFieldHintId(fieldId("unit"))}
                aria-invalid={fieldError?.field === "unit" || undefined}
                options={options}
                sort={false}
                value={draft.unit || null}
                onValueChange={(unit) => setDraft((d) => ({ ...d, unit }))}
                placeholder={t_i18n("Pick a unit of work")}
                emptyMessage={t_i18n("No unit matches.")}
              />
            </SettingsField>
            <div role="group" aria-labelledby={countLabelId} className="min-w-0 space-y-1.5">
              <div className="flex items-center gap-1.5">
                <span id={countLabelId} className="content-base font-medium leading-none text-default-primary">
                  {t_i18n("Counting mode")}
                </span>
                <InfoTip
                  label={t_i18n("About this field")}
                  content={
                    <div className="max-w-xs content-compact">
                      {t_i18n("How the units are counted once a run ends. Example: per run with 3 units per run counts 3 units each time a run finishes.")}
                    </div>
                  }
                />
              </div>
              <SegmentedFilter
                selection="radio"
                ariaLabel={t_i18n("Counting mode")}
                options={countModeOptions(structuredOutput)}
                value={draft.count_mode}
                onChange={(count_mode) => setDraft((d) => ({ ...d, count_mode }))}
                testId={`${testId}-count`}
              />
              <ul id={countHelpId} className="space-y-0.5 content-compact text-default-secondary">
                {countModeOptions(structuredOutput).map((option) => (
                  <li key={option.key}>{t_i18n(COUNT_MODE_MEANINGS[option.key])}</li>
                ))}
              </ul>
            </div>
            {draft.count_mode === "per_run" && (
              <SettingsField
                id={fieldId("units_per_count")}
                label={t_i18n("Units per run")}
                required
                error={errorOf("units_per_count")}
                info={t_i18n(
                  "Multiplies each finished run. Example: a run that triages 3 reports counts 3 units with 3 here. Empty: the declaration cannot be saved.",
                )}
                helper={t_i18n("How many units one finished run counts, a whole number from 1 to 10,000.")}
              >
                <Input
                  id={fieldId("units_per_count")}
                  aria-label={t_i18n("Units per run")}
                  type="number"
                  min={1}
                  max={10000}
                  step={1}
                  className="w-32"
                  value={draft.units_per_count}
                  aria-required
                  aria-invalid={fieldError?.field === "units_per_count" || undefined}
                  aria-describedby={settingsFieldHintId(fieldId("units_per_count"))}
                  onChange={(e) => setDraft((d) => ({ ...d, units_per_count: e.target.value }))}
                />
              </SettingsField>
            )}
            {draft.count_mode === "per_output_item" && (
              <SettingsField
                id={fieldId("count_path")}
                label={t_i18n("Output path")}
                required
                error={errorOf("count_path")}
                info={t_i18n(
                  "A dotted path into the run's JSON output. Example: result.contracts counts each item of that array. Empty: the declaration cannot be saved.",
                )}
                helper={t_i18n("The array of the run's JSON output whose items are counted.")}
              >
                <Input
                  id={fieldId("count_path")}
                  aria-label={t_i18n("Output path")}
                  className="max-w-sm"
                  placeholder="result.contracts"
                  value={draft.count_path}
                  aria-required
                  aria-invalid={fieldError?.field === "count_path" || undefined}
                  aria-describedby={settingsFieldHintId(fieldId("count_path"))}
                  onChange={(e) => setDraft((d) => ({ ...d, count_path: e.target.value }))}
                />
              </SettingsField>
            )}
            {saveError && (
              <p role="alert" className="content-compact text-feedback-error-primary">
                {saveError}
              </p>
            )}
            <div className="flex flex-wrap items-center justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => setEditing(false)} disabled={put.isPending}>
                {t_i18n("Cancel")}
              </Button>
              <Button type="button" onClick={save} disabled={put.isPending}>
                {t_i18n("Save outcomes")}
              </Button>
            </div>
          </div>
        ) : declared ? (
          <div className="space-y-3">
            {declared.origin && (
              <div className="flex items-center gap-2">
                <span className="content-compact text-default-secondary">{t_i18n("Origin")}</span>
                <RoiOriginBadge row={declared} />
              </div>
            )}
            {readOnlyRows(declared)}
            {builtinDefault && (
              <RoiDefaultDiff
                line={customizedLine(declared)}
                fields={[
                  { label: "Unit of work", default: unitName(builtinDefault.unit), current: unitName(declared.unit) },
                  { label: "Counting mode", default: describeCount(builtinDefault), current: describeCount(declared) },
                ]}
              />
            )}
            {editable && (
              <div className="flex flex-wrap items-center justify-end gap-2">
                {!shipped && (
                  <Button type="button" variant="outline" onClick={resetToInherited} disabled={remove.isPending}>
                    {builtinDefault ? t_i18n("Reset to default") : parents.length > 0 ? t_i18n("Reset to inherited") : t_i18n("Stop counting")}
                  </Button>
                )}
                <Button
                  ref={editButtonRef}
                  type="button"
                  variant="outline"
                  className="gap-1.5"
                  onClick={() => startEditing(declared)}
                >
                  <Pencil className="h-3.5 w-3.5" aria-hidden /> {shipped ? t_i18n("Customize") : t_i18n("Edit")}
                </Button>
              </div>
            )}
            {saveError && (
              <p role="alert" className="content-compact text-feedback-error-primary">
                {saveError}
              </p>
            )}
          </div>
        ) : inherited ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="content-compact text-default-secondary">
                {inherited.parent.kind === "fleet"
                  ? t_i18n("Inherited from fleet {name}", { name: inherited.parent.name })
                  : t_i18n("Inherited from agent {name}", { name: inherited.parent.name })}
              </p>
              {editable && (
                <Button type="button" variant="outline" onClick={() => startEditing(inherited.declaration)}>
                  {t_i18n("Customize")}
                </Button>
              )}
            </div>
            {readOnlyRows(inherited.declaration)}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="content-compact text-default-secondary">
                {editable
                  ? t_i18n("Not counted in ROI. Pick a unit to measure the time this work saves.")
                  : t_i18n("Not counted in ROI. Only someone who can edit it declares a unit of work.")}
              </p>
              {editable && (
                <Button type="button" variant={suggestion ? "outline" : "default"} onClick={() => startEditing(null)}>
                  {t_i18n("Pick a unit")}
                </Button>
              )}
            </div>
            {suggestion && (
              <RoiSuggestionRow
                suggestion={suggestion}
                unitName={unitName(suggestion.unit)}
                pending={put.isPending}
                onAccept={acceptSuggestion}
                testId={`${testId}-suggestion`}
              />
            )}
            {saveError && (
              <p role="alert" className="content-compact text-feedback-error-primary">
                {saveError}
              </p>
            )}
          </div>
        )}
    </>
  );

  if (chrome === "section") {
    const titleId = `${testId}-title`;
    return (
      <section data-testid={testId} aria-labelledby={titleId} className="space-y-3 border-t border-elevation-subtle-soft pt-4">
        <div className="flex items-center justify-between gap-2">
          <h3 id={titleId} className="flex items-center gap-1.5 content-compact font-semibold">
            <Target className="h-3.5 w-3.5 text-accent" aria-hidden />
            {t_i18n("Outcomes")}
          </h3>
          <EditionBadge className="content-caption" />
        </div>
        {body}
        {confirmDialog}
      </section>
    );
  }

  return (
    <Card data-testid={testId}>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
        <CardTitle className="content-base flex items-center gap-2">
          <Target className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Outcomes")}
        </CardTitle>
        <EditionBadge className="content-caption" />
      </CardHeader>
      <CardContent className="space-y-4 px-4 pb-4 pt-0">{body}</CardContent>
      {confirmDialog}
    </Card>
  );
}
