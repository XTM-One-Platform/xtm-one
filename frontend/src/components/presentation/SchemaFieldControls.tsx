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
 * The inspector's controls: one component per field descriptor of
 * `block-schema.ts` / `slide-schema.ts`, reading and writing the draft at a
 * spec path through the editor session.
 *
 * Every control writes through `editor.writeAt(slide, path, value, key)`, with
 * the field's path as the coalescing key so a word typed into one field is one
 * undo step. An optional field at its default is UNSET (`undefined`, which
 * `setAt` deletes) rather than written as `null` or `""`: the validator reads
 * a blank optional string as absent anyway, and a spec that spells no
 * defaults is what the agent writes too.
 */
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { INVALID_FIELD_CLASS } from "@/components/ui/invalid-field";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PresentationSlide } from "@/types";

import {
  MAX_TABLE_COLUMNS,
  MAX_TABLE_ROWS,
  enumShown,
  enumWritten,
  itemsWritten,
  type ItemsSchema,
  type SchemaField,
  type SchemaValue,
} from "./block-schema";
import { DeckIcon } from "./DeckIcon";
import { getDeckIconCatalog, loadDeckIconCatalog, resolveDeckIcon, subscribeDeckIconCatalog } from "./deck-icons";
import { childPath, getAt, parsePath, setAt } from "./edit-paths";
import { markupProblem } from "./markup";
import { TEXTAREA_CLASS, useMarkupProblemText } from "./markup-problems";
import type { DeckEditor } from "./presentation-editor";
import { TemplateAssetField } from "./TemplateAssetField";

/** Radix `Select` cannot hold an empty value: the "unset" option's value. */
const UNSET = "__unset__";

/**
 * Every inspector field to the Forms standard (Design standards s.2): the
 * label at body size (14 px), the help or warning line at 12 px, tied to its
 * control by `aria-describedby`. A field that explains itself (`info`) adds
 * its info bubble next to the label.
 */
const FIELD_LABEL = "content-base text-default-primary";
const FORM_FIELD_LABEL = FIELD_LABEL;
const FORM_FIELD_HELP = "content-compact leading-relaxed text-default-secondary";
const FIELD_WARNING = "content-compact leading-relaxed text-feedback-warning-primary";

/** A display label for an enum option (message ids for the common ones). */
function optionLabel(value: string | number, t: (id: string) => string): string {
  if (typeof value === "number") return String(value);
  const known: Record<string, string> = {
    sm: "Small",
    md: "Medium",
    lg: "Large",
    xs: "Extra small",
    xl: "Extra large",
    full: "Full width",
    start: "Start",
    center: "Center",
    end: "End",
    even: "Even",
    stretch: "Stretch",
    normal: "Normal",
    compact: "Compact",
    tight: "Tight",
    equal: "Equal",
    wide_left: "Wide left",
    wide_right: "Wide right",
    accent: "Accent",
    info: "Info",
    success: "Success",
    warning: "Warning",
    error: "Error",
    muted: "Muted",
    bullet: "Bullets",
    check: "Checks",
    arrow: "Arrows",
    cross: "Crosses",
    numbered: "Numbered",
    plain: "Plain",
    outline: "Outline",
    solid: "Solid",
    gradient: "Gradient",
    soft: "Soft",
    phases: "Phases",
    events: "Events",
    horizontal: "Horizontal",
    vertical: "Vertical",
    contain: "Contain",
    cover: "Cover",
    up: "Up",
    down: "Down",
    flat: "Flat",
    number: "Number",
    currency: "Currency",
    percent: "Percent",
    bar: "Bar",
    stacked_bar: "Stacked bar",
    line: "Line",
    area: "Area",
    pie: "Pie",
    donut: "Donut",
    gauge: "Gauge",
    combo: "Combo",
    waterfall: "Waterfall",
    butterfly: "Butterfly",
    map: "Map",
    world: "World",
    europe: "Europe",
    americas: "Americas",
    apac: "Asia-Pacific",
    ramp: "Ramp",
    classes: "Classes",
    list: "List",
    grid: "Grid",
    row: "Row by row",
    column: "Column by column",
    web: "Web",
    email: "Email",
    linkedin: "LinkedIn",
    x: "X",
    github: "GitHub",
    phone: "Phone",
    original: "Original",
    mono: "Monochrome",
    ink: "Ink",
    tinted: "Tinted",
    card: "Card",
    inline: "Inline",
    stacked: "Stacked",
    top: "Top",
    bottom: "Bottom",
    right: "Right",
    none: "None",
    browser: "Browser window",
    browser_slim: "Slim browser window",
    device: "Device",
    hero: "Hero",
    decorative: "Decorative",
  };
  return known[value] ? t(known[value]) : value;
}

/** An option's label in *field*: its own message id when it declares one, else the shared one. */
function fieldOptionLabel(field: SchemaField, value: string | number, t: (id: string) => string): string {
  const own = field.kind === "enum" ? field.optionLabels?.[String(value)] : undefined;
  return own ? t(own) : optionLabel(value, t);
}

export interface FieldScope {
  editor: DeckEditor;
  slide: number;
  /** The spec path the fields are relative to (`""` for the slide, a block path...). */
  base: string;
  /** The draft's template, where an `asset` field's pictures are picked from (`null`: a free deck). */
  templateId?: string | null;
}

/** One field, read from and written to the draft. */
export function SchemaFieldControl({
  field,
  scope,
  inactive,
  inactiveValues,
  unsetReads,
  sectionLabelled,
}: {
  field: SchemaField;
  scope: FieldScope;
  /**
   * The host draws the field's label itself as a section label (the slide
   * palette under THEME): the field's own label stays for assistive
   * technology only, so one column never shows two label styles.
   */
  sectionLabelled?: boolean;
  /**
   * Why the field has no visible effect on the slide as it stands
   * (`fieldInactiveReason`): said under the control, in place of the hint,
   * while the value stays editable - it takes effect the moment the slide
   * takes the form it applies to.
   */
  inactive?: string | null;
  inactiveValues?: Record<string, number>;
  /**
   * What an optional enum or flag reads as while it is unset, when the rest of
   * the block decides it (a quote's look follows its variant, `quoteLookDefault`):
   * named in the unset option - "Default (Extra large)", "Auto (On)" -
   * so the picker says what the slide draws. Picking a value still pins it.
   */
  unsetReads?: SchemaValue | boolean;
}) {
  const { t_i18n } = useFormatter();
  const problemText = useMarkupProblemText();
  const { editor, slide, base } = scope;
  const path = childPath(base, field.key);
  const slideSpec = editor.draft.slides[slide];
  const value = slideSpec ? getAt(slideSpec, parsePath(path)) : undefined;
  const write = (next: unknown) => editor.writeAt(slide, path, next, path);
  const blank = value === undefined || value === null || value === "";
  // A `whenSet` field shown once stays for as long as its block is selected:
  // emptied to be retyped, it must not unmount under the caret.
  const [shownWhenSet] = useState(!blank);
  if (field.whenSet && blank && !shownWhenSet) return null;
  // A number one mode reads (#10411 UX B2): hidden while its sibling picks another mode.
  if (field.shownWhen && slideSpec) {
    const sibling = getAt(slideSpec, parsePath(childPath(base, field.shownWhen.key)));
    if (sibling !== field.shownWhen.value) return null;
  }
  const id = `deck-field-${slide}-${path.replace(/\./g, "-")}`;
  const noteId = `${id}-note`;
  // A field that explains itself is drawn to the Forms standard (UX round 1 B2 of #9783).
  const standard = !!field.info;
  // The reason a control changes nothing here outranks its generic hint.
  const note = inactive ? (
    <p id={noteId} className={FIELD_WARNING} role="note">
      {t_i18n(inactive, inactiveValues)}
    </p>
  ) : field.hint ? (
    <p id={noteId} className={FORM_FIELD_HELP}>
      {t_i18n(field.hint)}
    </p>
  ) : null;

  switch (field.kind) {
    case "enum": {
      const shown = enumShown(field, value);
      // A required choice with no value shows the placeholder (Radix reads
      // `""` as "nothing chosen"); a look read in normal form shows its
      // default (`defaultValue`); every other enum offers "Default" (unset).
      const current = shown === undefined ? (field.required ? "" : UNSET) : String(shown);
      // A picker may mix words and numbers: the option written keeps its own type.
      const option = (next: string) => field.options.find((each) => String(each) === next) ?? next;
      const lookDefault = field.defaultValue !== undefined;
      const optionHint = (each: SchemaValue) => {
        const hint = field.optionHints?.[String(each)];
        return hint ? t_i18n(hint) : undefined;
      };
      return (
        <div className="space-y-1">
          {standard && !sectionLabelled ? (
            <div className="flex items-center gap-1">
              <Label htmlFor={id} className={FORM_FIELD_LABEL}>
                {t_i18n(field.label)}
              </Label>
              <InfoTip
                content={t_i18n(field.info as string)}
                label={t_i18n("What {fact} means", { fact: t_i18n(field.label) })}
              />
            </div>
          ) : (
            <Label htmlFor={id} className={sectionLabelled ? "sr-only" : FIELD_LABEL}>
              {t_i18n(field.label)}
            </Label>
          )}
          <SearchableSelect
            id={id}
            aria-label={t_i18n(field.label)}
            aria-describedby={note ? noteId : undefined}
            searchable="auto"
            sort={false}
            options={field.options.map((each) => ({
              value: String(each),
              label: fieldOptionLabel(field, each, t_i18n),
              description: optionHint(each),
            }))}
            pinned={
              !field.required && !lookDefault
                ? [{
                    value: UNSET,
                    description: field.unsetHint ? t_i18n(field.unsetHint) : undefined,
                    label:
                      unsetReads === undefined || typeof unsetReads === "boolean"
                        ? t_i18n("Default")
                        : t_i18n("Default ({value})", { value: fieldOptionLabel(field, unsetReads, t_i18n) }),
                  }]
                : []
            }
            value={current}
            onValueChange={(next) => write(enumWritten(field, next === UNSET ? undefined : option(next)))}
            placeholder={field.required ? t_i18n("Choose...") : t_i18n("Default")}
          />
          {note}
        </div>
      );
    }
    case "flag": {
      if (field.optional) {
        // A tri-state (`bool | None`): auto / on / off.
        const current = value === undefined || value === null ? UNSET : value ? "true" : "false";
        return (
          <div className="space-y-1">
            <Label htmlFor={id} className={FIELD_LABEL}>
              {t_i18n(field.label)}
            </Label>
            <Select value={current} onValueChange={(next) => write(next === UNSET ? undefined : next === "true")}>
              <SelectTrigger
                id={id}
                className="w-full"
                aria-label={t_i18n(field.label)}
                aria-describedby={note ? noteId : undefined}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNSET} className="content-compact italic">
                  {typeof unsetReads === "boolean"
                    ? t_i18n("Auto ({value})", { value: unsetReads ? t_i18n("On") : t_i18n("Off") })
                    : t_i18n("Auto")}
                </SelectItem>
                <SelectItem value="true" className="content-compact">
                  {t_i18n("On")}
                </SelectItem>
                <SelectItem value="false" className="content-compact">
                  {t_i18n("Off")}
                </SelectItem>
              </SelectContent>
            </Select>
            {note}
          </div>
        );
      }
      return (
        <div className="flex items-center justify-between gap-3 py-0.5">
          <div className="min-w-0">
            <Label htmlFor={id} className={FIELD_LABEL}>
              {t_i18n(field.label)}
            </Label>
            {note}
          </div>
          {/* Unset reads as the model's default (`defaultOn` for the few flags
              that default to true), and a switch put back on its default is
              unset again - the spec spells no defaults, like the agent's. */}
          <Switch
            id={id}
            aria-describedby={note ? noteId : undefined}
            checked={value === undefined || value === null ? !!field.defaultOn : value === true}
            onCheckedChange={(checked) => write(checked === !!field.defaultOn ? undefined : checked)}
          />
        </div>
      );
    }
    case "number": {
      return (
        <div className="space-y-1">
          <Label htmlFor={id} className={FIELD_LABEL}>
            {t_i18n(field.label)}
          </Label>
          <Input aria-label={t_i18n(field.label)}
            id={id}
            type="number"
            className="h-8 content-compact"
            min={field.min}
            max={field.max}
            aria-describedby={field.hint ? noteId : undefined}
            value={typeof value === "number" ? value : ""}
            placeholder={field.optional ? t_i18n("Default") : undefined}
            onChange={(event) => {
              const raw = event.target.value;
              if (raw === "") {
                write(undefined);
                return;
              }
              const parsed = Number(raw);
              if (Number.isFinite(parsed)) write(parsed);
            }}
          />
          {field.hint && (
            <p id={noteId} className={FORM_FIELD_HELP}>
              {t_i18n(field.hint)}
            </p>
          )}
        </div>
      );
    }
    case "text": {
      const text = typeof value === "string" ? value : "";
      const problem = field.markup ? problemText(markupProblem(text)) : null;
      const onChange = (next: string) => write(next === "" && field.optional ? undefined : next);
      return (
        <div className="space-y-1">
          <Label htmlFor={id} className={FIELD_LABEL}>
            {t_i18n(field.label)}
          </Label>
          {field.multiline ? (
            <textarea
              id={id}
              className={cn(TEXTAREA_CLASS, INVALID_FIELD_CLASS.alert)}
              rows={3}
              value={text}
              onChange={(event) => onChange(event.target.value)}
              aria-invalid={problem ? true : undefined}
              aria-describedby={problem || note ? noteId : undefined}
            />
          ) : (
            <Input aria-label={t_i18n(field.label)}
              id={id}
              className="h-8 content-compact"
              invalidTone="alert"
              value={text}
              onChange={(event) => onChange(event.target.value)}
              aria-invalid={problem ? true : undefined}
              aria-describedby={problem || note ? noteId : undefined}
            />
          )}
          {problem ? (
            <p id={noteId} className={FIELD_WARNING} role="status">
              {problem}
            </p>
          ) : (
            note
          )}
        </div>
      );
    }
    case "icon":
      return (
        <div className="space-y-1">
          <IconField
            id={id}
            label={t_i18n(field.label)}
            value={typeof value === "string" ? value : ""}
            optional={!!field.optional}
            onChange={(next) => write(next === "" ? undefined : next)}
          />
          {note}
        </div>
      );
    case "asset":
      return (
        <div className="space-y-1">
          <TemplateAssetField
            label={field.label}
            value={typeof value === "string" && value ? value : undefined}
            templateId={scope.templateId ?? null}
            roles={field.roles}
            onChange={(next) => write(next)}
          />
          {note}
        </div>
      );
    case "strings": {
      const items = Array.isArray(value)
        ? (value as unknown[]).map((item) => (typeof item === "string" ? item : ""))
        : [];
      const set = (next: string[]) => write(next.length ? next : undefined);
      return (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <Label className={FIELD_LABEL}>{t_i18n(field.label)}</Label>
            <Button
              variant="secondary"
              className="gap-1 px-2"
              disabled={items.length >= field.max}
              onClick={() => set([...items, t_i18n("New line")])}
            >
              <Plus className="h-3 w-3" /> {t_i18n("Add")}
            </Button>
          </div>
          <ol className="space-y-1">
            {items.map((item, index) => {
              const problem = problemText(markupProblem(item));
              return (
                <li key={index} className="flex items-start gap-1">
                  <Input
                    className="h-7 content-compact"
                    invalidTone="alert"
                    aria-invalid={problem ? true : undefined}
                    value={item}
                    aria-label={t_i18n("{label} {n}", {
                      label: t_i18n(field.label),
                      n: index + 1,
                    })}
                    onChange={(event) => set(items.map((s, i) => (i === index ? event.target.value : s)))}
                  />
                  <RowButtons
                    canUp={index > 0}
                    canDown={index < items.length - 1}
                    onUp={() => set(swap(items, index, index - 1))}
                    onDown={() => set(swap(items, index, index + 1))}
                    onRemove={() => set(items.filter((_, i) => i !== index))}
                  />
                </li>
              );
            })}
          </ol>
        </div>
      );
    }
    case "icons": {
      const items = Array.isArray(value)
        ? (value as unknown[]).map((item) => (typeof item === "string" ? item : ""))
        : [];
      // The model's list has a default factory: the stored spec spells an
      // empty strip `[]`, so an emptied one is written that way too.
      const set = (next: string[]) => write(next);
      return (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <Label className={FIELD_LABEL}>
              {t_i18n(field.label)}{" "}
              <span className="tabular-nums">
                ({items.length}/{field.max})
              </span>
            </Label>
            <Button
              variant="secondary"
              className="gap-1 px-2"
              disabled={items.length >= field.max}
              onClick={() => set([...items, "sparkles"])}
            >
              <Plus className="h-3 w-3" /> {t_i18n("Add")}
            </Button>
          </div>
          <ol className="space-y-1.5">
            {items.map((item, index) => (
              <li key={index} className="flex items-end gap-1">
                <div className="min-w-0 flex-1">
                  <IconField
                    id={`${id}-${index}`}
                    label={t_i18n("{label} {n}", { label: t_i18n(field.label), n: index + 1 })}
                    value={item}
                    optional={false}
                    onChange={(next) => set(items.map((each, i) => (i === index ? next : each)))}
                  />
                </div>
                <RowButtons
                  canUp={index > 0}
                  canDown={index < items.length - 1}
                  onUp={() => set(swap(items, index, index - 1))}
                  onDown={() => set(swap(items, index, index + 1))}
                  onRemove={() => set(items.filter((_, i) => i !== index))}
                />
              </li>
            ))}
          </ol>
          {note}
        </div>
      );
    }
    case "group": {
      const present = value !== undefined && value !== null;
      if (!present && field.optional) {
        return (
          <div className="flex items-center justify-between gap-3 py-0.5">
            <span className={FIELD_LABEL}>{t_i18n(field.label)}</span>
            <Button
              variant="secondary"
              className="gap-1 px-2"
              onClick={() => write(groupDefault(field))}
            >
              <Plus className="h-3 w-3" /> {t_i18n("Add")}
            </Button>
          </div>
        );
      }
      return (
        <fieldset className="space-y-2 rounded-sm border border-elevation-subtle-soft p-2">
          <legend className="flex items-center gap-2 px-1 content-base font-medium text-default-primary">
            {t_i18n(field.label)}
            {field.optional && (
              <button
                type="button"
                className="content-compact font-normal text-default-secondary underline-offset-2 hover:underline"
                onClick={() => write(undefined)}
              >
                {t_i18n("Remove")}
              </button>
            )}
          </legend>
          {field.fields.map((child) => (
            <SchemaFieldControl key={child.key} field={child} scope={{ ...scope, base: path }} />
          ))}
        </fieldset>
      );
    }
    case "objects":
      // A list of objects inside an item (a configuration card's rows): the
      // block's own items editor, one level down.
      return (
        <ItemsEditor
          schema={{
            key: field.key,
            label: field.label,
            min: field.min,
            max: field.max,
            fields: field.fields,
            create: field.create,
          }}
          scope={scope}
        />
      );
    default:
      return null;
  }
}

/** A minimal valid object for an optional group: its first text field filled. */
function groupDefault(field: Extract<SchemaField, { kind: "group" }>): Record<string, unknown> {
  const object: Record<string, unknown> = {};
  for (const child of field.fields) {
    if (child.kind === "text" && !child.optional) object[child.key] = child.label;
  }
  return object;
}

function swap<T>(items: T[], from: number, to: number): T[] {
  if (to < 0 || to >= items.length) return items;
  const next = [...items];
  [next[from], next[to]] = [next[to], next[from]];
  return next;
}

function RowButtons({
  canUp,
  canDown,
  canRemove = true,
  onUp,
  onDown,
  onRemove,
}: {
  canUp: boolean;
  canDown: boolean;
  canRemove?: boolean;
  onUp: () => void;
  onDown: () => void;
  onRemove: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex shrink-0 items-center">
      <Tip content={t_i18n("Move up")} wrap={!canUp}>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          disabled={!canUp}
          onClick={onUp}
          aria-label={t_i18n("Move up")}
        >
          <ArrowUp className="h-3 w-3" />
        </Button>
      </Tip>
      <Tip content={t_i18n("Move down")} wrap={!canDown}>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          disabled={!canDown}
          onClick={onDown}
          aria-label={t_i18n("Move down")}
        >
          <ArrowDown className="h-3 w-3" />
        </Button>
      </Tip>
      <Tip content={t_i18n("Remove")} wrap={!canRemove}>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0 text-default-secondary hover:text-feedback-error-primary"
          disabled={!canRemove}
          onClick={onRemove}
          aria-label={t_i18n("Remove")}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </Tip>
    </span>
  );
}

/** Lucide's kebab id for an export name (`ShieldCheck` -> `shield-check`). */
function kebabOf(exportName: string): string {
  return exportName.replace(/([a-z])([A-Z])/g, "$1-$2").toLowerCase();
}

/**
 * An icon field: the kebab-case Lucide id, previewed live and matched against
 * the catalog the deck draws from (`deck-icons.ts`), with suggestions as the
 * name is typed. An id the catalog lacks is said so before the save refuses it.
 */
export function IconField({
  id,
  label,
  value,
  optional,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  optional: boolean;
  onChange: (next: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const catalog = useSyncExternalStore(subscribeDeckIconCatalog, getDeckIconCatalog, getDeckIconCatalog);
  useEffect(() => {
    void loadDeckIconCatalog().catch(() => undefined);
  }, []);
  const [focused, setFocused] = useState(false);
  const known = !!value && resolveDeckIcon(catalog, value) !== null;
  const suggestions = useMemo(() => {
    if (!catalog || !value || known) return [];
    const needle = value.toLowerCase().replace(/-/g, "");
    if (needle.length < 2) return [];
    const out: string[] = [];
    for (const name of Object.keys(catalog)) {
      // Names with digits (`arrow-down-0-1`) have no lossless kebab form here.
      if (/\d/.test(name) || name.endsWith("Icon") || name.startsWith("Lucide")) continue;
      if (name.toLowerCase().includes(needle)) {
        out.push(kebabOf(name));
        if (out.length >= 12) break;
      }
    }
    return out;
  }, [catalog, value, known]);
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className={FIELD_LABEL}>
        {label}
      </Label>
      <div className="relative flex items-center gap-2">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm border border-elevation-subtle-soft bg-elevation-default text-accent">
          {known ? (
            <DeckIcon name={value} className="h-4 w-4" />
          ) : (
            <span className="content-compact text-default-secondary">-</span>
          )}
        </span>
        <Input aria-label={label}
          id={id}
          className="h-8 font-mono-plex content-compact"
          invalidTone="alert"
          aria-invalid={(value && !known && catalog) ? true : undefined}
          aria-describedby={value && !known && catalog ? `${id}-unknown` : undefined}
          value={value}
          placeholder={optional ? t_i18n("None") : "shield-check"}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value.trim().toLowerCase())}
          onFocus={() => setFocused(true)}
          onBlur={() => window.setTimeout(() => setFocused(false), 150)}
        />
        {focused && suggestions.length > 0 && (
          <ul
            role="listbox"
            className="absolute left-10 right-0 top-full z-20 mt-1 max-h-48 overflow-y-auto rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-1 shadow-overlay backdrop-blur-sm"
          >
            {suggestions.map((name) => (
              <li key={name} role="option" aria-selected={false}>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left font-mono-plex content-compact hover:bg-elevation-hover"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    onChange(name);
                    setFocused(false);
                  }}
                >
                  <DeckIcon name={name} className="h-3.5 w-3.5 text-accent" />
                  {name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {value && !known && catalog && (
        <p id={`${id}-unknown`} className={FIELD_WARNING} role="status">
          {t_i18n("No icon with this name in the catalog.")}
        </p>
      )}
    </div>
  );
}

/**
 * A list of items (bullets, tiles, steps, tiers...): each row shows the item's
 * first text inline and unfolds to every field; add, remove and reorder within
 * the model's bounds.
 */
/**
 * The items of a block (a list's bullets, a grid's tiles, a table's headers)
 * with their fields. `fixed` keeps the list's SHAPE out of reach - no add, no
 * reorder, no remove - for a list another editor owns structurally: a table's
 * headers are one per column, and the column is added or removed by the
 * `TableEditor`, which grows or trims every row with it. `inactive` says why
 * an item field changes nothing on the block as it stands (a plain grid's
 * tile adornments, `blockItemFieldInactiveReason`).
 */
export function ItemsEditor({
  schema,
  scope,
  fixed = false,
  inactive,
}: {
  schema: ItemsSchema;
  scope: FieldScope;
  fixed?: boolean;
  inactive?: (field: SchemaField) => string | null;
}) {
  const { t_i18n } = useFormatter();
  const { editor, slide, base } = scope;
  const path = childPath(base, schema.key);
  const slideSpec = editor.draft.slides[slide];
  const raw = slideSpec ? getAt(slideSpec, parsePath(path)) : undefined;
  const items = Array.isArray(raw) ? (raw as unknown[]) : [];
  const [open, setOpen] = useState<number | null>(null);
  const set = (next: unknown[]) => editor.writeAt(slide, path, itemsWritten(schema, next));
  const headline = schema.fields.find((f) => f.kind === "text");
  const problemText = useMarkupProblemText();
  // A list whose item the inspector cannot make valid on its own (a logo's
  // file) has no factory: it is edited, reordered and trimmed, never grown.
  const { create } = schema;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <Label className={FIELD_LABEL}>
          {t_i18n(schema.label)}{" "}
          {!fixed && (
            <span className="tabular-nums">
              ({items.length}/{schema.max})
            </span>
          )}
        </Label>
        {!fixed && create && (
          <Button
            variant="secondary"
            className="gap-1 px-2"
            disabled={items.length >= schema.max}
            onClick={() => {
              set([...items, create()]);
              setOpen(items.length);
            }}
          >
            <Plus className="h-3 w-3" /> {t_i18n("Add")}
          </Button>
        )}
      </div>
      <ol className="space-y-1">
        {items.map((item, index) => {
          const isOpen = open === index;
          const record = (item ?? {}) as Record<string, unknown>;
          const headlineValue =
            headline && typeof record[headline.key] === "string" ? (record[headline.key] as string) : "";
          const problem =
            headline?.kind === "text" && headline.markup ? problemText(markupProblem(headlineValue)) : null;
          return (
            <li key={index} className="rounded-sm border border-elevation-subtle-soft bg-elevation-default">
              <div className="flex items-center gap-1 px-1 py-1">
                <button
                  type="button"
                  className="flex h-7 w-6 shrink-0 items-center justify-center text-default-secondary"
                  onClick={() => setOpen(isOpen ? null : index)}
                  aria-expanded={isOpen}
                  aria-label={isOpen ? t_i18n("Collapse") : t_i18n("Expand")}
                >
                  {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                </button>
                <span className="w-4 shrink-0 content-compact tabular-nums text-default-secondary">{index + 1}</span>
                {headline ? (
                  <Input
                    className="h-7 min-w-0 flex-1 content-compact"
                    invalidTone="alert"
                    aria-invalid={problem ? true : undefined}
                    value={headlineValue}
                    aria-label={t_i18n("{label} {n}", {
                      label: t_i18n(schema.label),
                      n: index + 1,
                    })}
                    onChange={(event) =>
                      editor.writeAt(
                        slide,
                        childPath(path, index, headline.key),
                        event.target.value,
                        childPath(path, index, headline.key),
                      )
                    }
                  />
                ) : (
                  <span className="flex-1 truncate content-compact text-default-secondary">
                    {t_i18n("Item {n}", { n: index + 1 })}
                  </span>
                )}
                {!fixed && (
                  <RowButtons
                    canUp={index > 0}
                    canDown={index < items.length - 1}
                    canRemove={items.length > schema.min}
                    onUp={() => {
                      set(swap(items, index, index - 1));
                      if (isOpen) setOpen(index - 1);
                    }}
                    onDown={() => {
                      set(swap(items, index, index + 1));
                      if (isOpen) setOpen(index + 1);
                    }}
                    onRemove={() => {
                      set(items.filter((_, i) => i !== index));
                      setOpen(null);
                    }}
                  />
                )}
              </div>
              {isOpen && (
                <div className="space-y-2 border-t border-elevation-subtle-soft px-2 py-2">
                  {schema.fields
                    .filter((field) => field !== headline)
                    .map((field) => (
                      <SchemaFieldControl
                        key={field.key}
                        field={field}
                        scope={{ ...scope, base: childPath(path, index) }}
                        inactive={inactive?.(field)}
                      />
                    ))}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * The `table` block's cells: a grid of inputs, rows added and removed here,
 * columns through the headers list (a header added or removed adds or removes
 * its cell on every row, so the rows always match the headers).
 */
export function TableEditor({ scope }: { scope: FieldScope }) {
  const { t_i18n } = useFormatter();
  const { editor, slide, base } = scope;
  const slideSpec = editor.draft.slides[slide];
  const headersRaw = slideSpec ? getAt(slideSpec, parsePath(childPath(base, "headers"))) : undefined;
  const rowsRaw = slideSpec ? getAt(slideSpec, parsePath(childPath(base, "rows"))) : undefined;
  const headers = Array.isArray(headersRaw) ? (headersRaw as { text?: string }[]) : [];
  const rows = Array.isArray(rowsRaw) ? (rowsRaw as { text?: string }[][]) : [];
  const setRows = (next: { text?: string }[][]) => editor.writeAt(slide, childPath(base, "rows"), next);
  const setHeaders = (nextHeaders: { text?: string }[], nextRows: { text?: string }[][]) =>
    editor.apply((d) => {
      const s = d.slides[slide];
      if (!s) return d;
      let updated = s;
      updated = setAtSlide(updated, childPath(base, "headers"), nextHeaders);
      updated = setAtSlide(updated, childPath(base, "rows"), nextRows);
      return {
        ...d,
        slides: d.slides.map((x, i) => (i === slide ? updated : x)),
      };
    });

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <Label className={FIELD_LABEL}>
          {t_i18n("Cells")}{" "}
          <span className="tabular-nums">
            ({rows.length} x {headers.length})
          </span>
        </Label>
        <div className="flex items-center gap-1">
          <Button
            variant="secondary"
            className="gap-1 px-2"
            disabled={headers.length >= MAX_TABLE_COLUMNS}
            onClick={() =>
              setHeaders(
                [...headers, { text: t_i18n("Column") }],
                rows.map((row) => [...row, { text: "" }]),
              )
            }
          >
            <Plus className="h-3 w-3" /> {t_i18n("Column")}
          </Button>
          <Button
            variant="secondary"
            className="gap-1 px-2"
            disabled={rows.length >= MAX_TABLE_ROWS}
            onClick={() => setRows([...rows, headers.map(() => ({ text: "" }))])}
          >
            <Plus className="h-3 w-3" /> {t_i18n("Row")}
          </Button>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0.5 content-compact">
          <thead>
            <tr>
              <th />
              {headers.map((_, column) => (
                <th key={column} className="text-center font-normal text-default-secondary">
                  <Tip content={t_i18n("Remove column")} wrap={headers.length <= 1}>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 p-0 text-default-secondary hover:text-feedback-error-primary"
                      disabled={headers.length <= 1}
                      aria-label={t_i18n("Remove column")}
                      onClick={() =>
                        setHeaders(
                          headers.filter((_, c) => c !== column),
                          rows.map((row) => row.filter((_, c) => c !== column)),
                        )
                      }
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </Tip>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                <td className="w-6 pr-1 text-center">
                  <Tip content={t_i18n("Remove row")} wrap={rows.length <= 1}>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 p-0 text-default-secondary hover:text-feedback-error-primary"
                      disabled={rows.length <= 1}
                      aria-label={t_i18n("Remove row")}
                      onClick={() => setRows(rows.filter((_, i) => i !== r))}
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </Tip>
                </td>
                {headers.map((_, c) => (
                  <td key={c}>
                    <Input
                      className="h-7 min-w-[72px] content-compact"
                      value={row[c]?.text ?? ""}
                      aria-label={t_i18n("Row {r}, column {c}", {
                        r: r + 1,
                        c: c + 1,
                      })}
                      onChange={(event) =>
                        editor.writeAt(
                          slide,
                          childPath(base, "rows", r, c, "text"),
                          event.target.value,
                          childPath(base, "rows", r, c, "text"),
                        )
                      }
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** A write on one slide by string path (the two writes of `setHeaders` share one draft update). */
function setAtSlide(slide: PresentationSlide, path: string, value: unknown): PresentationSlide {
  return setAt(slide, parsePath(path), value);
}
