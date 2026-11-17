// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { AlertTriangle, ListPlus, Minus, Plus, RefreshCw, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import {
  ANNOUNCEABLE_PROVIDERS,
  DIRECTORY_VIEWS,
  filterDirectory,
  inferProvider,
  keyboardRow,
  kindOf,
  MAX_ANNOUNCED_MODELS,
  MODEL_ID_MAX_CHARS,
  naturalKind,
  parseModelList,
  resolveModel,
  typedModelProblem,
  viewCounts,
  withKind,
  type AnnouncedKind,
  type DirectoryModel,
  type DirectoryView,
  type ModelOrigin,
  type ModelSelection,
} from "@/lib/model-announcements";
import { cn } from "@/lib/utils";
import type { ProviderCatalogSummary } from "@/types";
import { Spinner } from "@filigran/design-system";

/** Rows the list renders at once: a search narrows the rest. */
const ROWS_SHOWN = 150;
// Radix Select items cannot carry an empty value.
const ALL_PROVIDERS = "__all__";
const NO_PROVIDER = "__none__";

/** Catalog keys, translated where they are shown. @i18n-keys */
const VIEW_LABELS = {
  all: "All",
  offered: "Users can pick",
  unoffered: "Not offered",
  chosen: "In the email",
} as const satisfies Record<DirectoryView, string>;

/** Where a model of the list comes from. @i18n-keys */
const ORIGIN_LABELS = {
  offered: "Offered",
  formerly: "Formerly offered",
  listed: "Listed by the provider",
  typed: "Added by hand",
} as const satisfies Record<ModelOrigin, string>;

/** The providers' own model lists, as the editor was able to load them. */
export interface ProviderCatalogsState {
  /** `null` when they are not asked for (AI off). */
  providers: ProviderCatalogSummary[] | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  onRefresh: () => void;
}

/**
 * Every model the admin can announce, searchable: the ones users can pick
 * (new or retired), the ones they no longer can, the ones a provider lists,
 * and any id typed or pasted by hand (retired only).
 */
export function ModelDirectory({
  directory,
  selection,
  onChange,
  providerLabel,
  catalogs,
}: {
  directory: DirectoryModel[];
  selection: ModelSelection;
  onChange: (selection: ModelSelection) => void;
  providerLabel: (provider: string) => string;
  catalogs: ProviderCatalogsState;
}) {
  const { t_i18n } = useFormatter();
  const baseId = useId();
  const listRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [view, setView] = useState<DirectoryView>("all");
  const [provider, setProvider] = useState<string | null>(null);
  /** Where the arrows took the keyboard (`-1` the typed or pasted models above the list), `null` until they do. */
  const [active, setActive] = useState<number | null>(null);
  const [typedProvider, setTypedProvider] = useState<string | null>(null);

  const listIds = useMemo(() => parseModelList(query), [query]);
  const isList = listIds.length > 1;
  const typed = isList ? "" : query.trim();
  const pool = useMemo(() => {
    if (!isList) return directory;
    const ids = new Set(listIds);
    return directory.filter((m) => ids.has(m.id));
  }, [directory, isList, listIds]);
  const search = isList ? "" : query;
  const shown = useMemo(
    () => filterDirectory(pool, { query: search, view, provider, selection, providerLabel }),
    [pool, search, view, provider, selection, providerLabel],
  );
  const counts = useMemo(
    () => viewCounts(pool, { query: search, provider, selection, providerLabel }),
    [pool, search, provider, selection, providerLabel],
  );
  const providers = useMemo(() => [...new Set(directory.map((m) => m.provider))], [directory]);
  const rows = shown.slice(0, ROWS_SHOWN);

  const typedVisible = !!typed && !directory.some((m) => m.id === typed);
  const typedProblem = typedVisible ? typedModelProblem(typed) : null;
  const typedModel = { id: typed, provider: typedProvider ?? inferProvider(typed) };
  const listModels = isList ? listIds.filter((id) => !typedModelProblem(id)).map((id) => resolveModel(id, directory)) : [];
  const listOffered = listModels.filter((m) => m.origin === "offered").length;
  const hasPanel = typedVisible || isList;
  const activeIndex = keyboardRow(active, rows.length, isList ? "list" : typedVisible ? "typed" : null);
  const optionId = (index: number) => `${baseId}-option-${index}`;
  const addable = shown.filter((m) => kindOf(selection, m.id) === null);
  const narrowed = !!search.trim() || provider !== null;

  useEffect(() => {
    if (activeIndex >= 0) listRef.current?.querySelector(`[data-option-index="${activeIndex}"]`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const changeQuery = (next: string) => {
    setQuery(next);
    setActive(null);
    setTypedProvider(null);
  };
  const setKind = (model: DirectoryModel, kind: AnnouncedKind | null) => onChange(withKind(selection, model, kind));
  const toggle = (model: DirectoryModel, kind: AnnouncedKind) => setKind(model, kindOf(selection, model.id) === kind ? null : kind);
  const toggleNatural = (model: DirectoryModel) => setKind(model, kindOf(selection, model.id) ? null : naturalKind(model));
  const addTyped = () => {
    if (!typed || typedProblem) return;
    onChange(withKind(selection, typedModel, "retired"));
    changeQuery("");
  };
  const addList = (retireAll: boolean) => {
    let next = selection;
    for (const model of listModels) next = withKind(next, model, retireAll ? "retired" : naturalKind(model));
    onChange(next);
    changeQuery("");
  };
  const addShown = () => {
    let next = selection;
    for (const model of addable) next = withKind(next, model, naturalKind(model));
    onChange(next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive(Math.min(activeIndex + 1, rows.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(activeIndex <= 0 ? (hasPanel ? -1 : 0) : activeIndex - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (activeIndex >= 0) {
        const model = rows[activeIndex];
        if (event.shiftKey) toggle(model, "retired");
        else toggleNatural(model);
      } else if (typedVisible) addTyped();
      else if (isList) addList(event.shiftKey);
    } else if (event.key === "Escape" && query) {
      // The dialog leaves an Escape on a search with text to it (`data-clear-on-escape`).
      changeQuery("");
    }
  };
  // A text field drops the new lines of a pasted list: they become commas.
  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData("text");
    if (!/[\r\n]/.test(text)) return;
    event.preventDefault();
    const input = event.currentTarget;
    const start = input.selectionStart ?? query.length;
    const end = input.selectionEnd ?? query.length;
    changeQuery(query.slice(0, start) + parseModelList(text).join(", ") + query.slice(end));
  };

  const groups: { provider: string; start: number; models: DirectoryModel[] }[] = [];
  rows.forEach((model, index) => {
    const last = groups[groups.length - 1];
    if (last && last.provider === model.provider) last.models.push(model);
    else groups.push({ provider: model.provider, start: index, models: [model] });
  });

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-default-secondary" />
        <Input
          value={query}
          role="combobox"
          aria-expanded
          aria-controls={`${baseId}-listbox`}
          aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
          aria-autocomplete="list"
          aria-label={t_i18n("Search the models")}
          data-clear-on-escape=""
          placeholder={t_i18n("Search a model or a provider, or paste a list of ids")}
          className="h-9 pl-8 content-compact"
          onChange={(event) => changeQuery(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedFilter
          ariaLabel={t_i18n("Which models")}
          value={view}
          onChange={(next) => {
            setView(next);
            setActive(null);
          }}
          options={DIRECTORY_VIEWS.map((key) => ({ key, label: VIEW_LABELS[key], count: counts[key] }))}
        />
        <SearchableSelect
          aria-label={t_i18n("Provider")}
          className="w-52"
          options={providers.map((p) => ({ value: p || NO_PROVIDER, label: providerLabel(p), keywords: [p] }))}
          pinned={[{ value: ALL_PROVIDERS, label: t_i18n("All providers") }]}
          value={provider === null ? ALL_PROVIDERS : provider || NO_PROVIDER}
          onValueChange={(value) => {
            setProvider(value === ALL_PROVIDERS ? null : value === NO_PROVIDER ? "" : value);
            setActive(null);
          }}
        />
      </div>

      {typedVisible && (
        <div
          className={cn(
            "space-y-2 rounded-sm border border-dashed border-elevation-subtle-soft p-2.5",
            activeIndex < 0 && "border-accent/50 bg-accent/[0.04]",
          )}
        >
          <div className="flex flex-wrap items-center gap-2">
            <ListPlus className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
            <p className="min-w-0 flex-1 content-compact">
              {t_i18n("{model} is in none of the lists: add it as retired.", { model: typed })}
            </p>
            <SearchableSelect
              aria-label={t_i18n("Provider named in the email")}
              className="w-52"
              options={ANNOUNCEABLE_PROVIDERS.map((p) => ({ value: p, label: providerLabel(p), keywords: [p] }))}
              pinned={[{ value: NO_PROVIDER, label: t_i18n("Other") }]}
              value={typedModel.provider || NO_PROVIDER}
              onValueChange={(value) => setTypedProvider(value === NO_PROVIDER ? "" : value)}
            />
            <Button size="sm" variant="secondary" className="h-7 gap-1 content-compact" disabled={!!typedProblem} onClick={addTyped}>
              <Minus className="h-3 w-3" /> {t_i18n("Retire")}
            </Button>
          </div>
          {typedProblem ? (
            <p role="alert" className="content-caption leading-relaxed text-feedback-warning-primary">
              {t_i18n("A model id is at most {max} letters, digits and . _ : / @ + - characters, starting with a letter or a digit.", { max: MODEL_ID_MAX_CHARS })}
            </p>
          ) : (
            <p className="text-[11px] leading-relaxed text-default-secondary">
              {activeIndex < 0
                ? t_i18n("The users cannot pick it, so it can only be announced as retired. Press Enter to add it.")
                : t_i18n("The users cannot pick it, so it can only be announced as retired. Press the Up arrow, then Enter, to add it.")}
            </p>
          )}
        </div>
      )}

      {isList && (
        <div
          className={cn(
            "space-y-2 rounded-sm border border-dashed border-elevation-subtle-soft p-2.5",
            activeIndex < 0 && "border-accent/50 bg-accent/[0.04]",
          )}
        >
          <div className="flex flex-wrap items-center gap-2">
            <ListPlus className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
            <p className="min-w-0 flex-1 content-compact">
              {t_i18n("{count, plural, one {# model pasted} other {# models pasted}}: {offered} the users can pick, {others} they cannot.", {
                count: listModels.length,
                offered: listOffered,
                others: listModels.length - listOffered,
              })}
            </p>
            <Tip content={t_i18n("Models the users can pick are added as new, the others as retired.")}>
              <Button size="sm" variant="secondary" className="h-7 gap-1 content-compact" disabled={listModels.length === 0} onClick={() => addList(false)}>
                <Plus className="h-3 w-3" /> {t_i18n("Add all")}
              </Button>
            </Tip>
            <Button size="sm" variant="ghost" className="h-7 gap-1 content-compact" disabled={listModels.length === 0} onClick={() => addList(true)}>
              <Minus className="h-3 w-3" /> {t_i18n("Retire all")}
            </Button>
          </div>
          {listIds.length > listModels.length && (
            <p role="alert" className="content-caption leading-relaxed text-feedback-warning-primary">
              {t_i18n("{count, plural, one {# entry is skipped} other {# entries are skipped}}: a model id is at most {max} letters, digits and . _ : / @ + - characters, starting with a letter or a digit.", {
                count: listIds.length - listModels.length,
                max: MODEL_ID_MAX_CHARS,
              })}
            </p>
          )}
        </div>
      )}

      <div
        ref={listRef}
        id={`${baseId}-listbox`}
        role="listbox"
        aria-multiselectable
        aria-label={t_i18n("Models")}
        className="h-[22rem] overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default"
      >
        {groups.map((group) => (
          <div key={`${group.provider}-${group.start}`} role="group" aria-labelledby={`${baseId}-group-${group.start}`}>
            <div
              id={`${baseId}-group-${group.start}`}
              role="presentation"
              className="sticky top-0 z-10 flex items-center justify-between border-b border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-1 content-caption font-semibold uppercase tracking-wide text-default-secondary backdrop-blur-sm"
            >
              <span className="truncate">{providerLabel(group.provider)}</span>
              <span className="tabular-nums font-normal">{group.models.length}</span>
            </div>
            {group.models.map((model, offset) => {
              const index = group.start + offset;
              const kind = kindOf(selection, model.id);
              return (
                <div
                  key={model.id}
                  id={optionId(index)}
                  data-option-index={index}
                  role="option"
                  aria-selected={kind !== null}
                  // Keeps the focus in the search, so the keyboard goes on from the row clicked.
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setActive(index);
                    toggleNatural(model);
                  }}
                  className={cn(
                    // The margin keeps a row scrolled to from the top under its sticky provider header.
                    "flex scroll-mt-7 cursor-pointer items-center gap-2 px-3 py-1.5 content-compact transition-colors hover:bg-elevation-hover",
                    index === activeIndex && "bg-elevation-hover hover:bg-elevation-highlight",
                  )}
                >
                  <span
                    aria-hidden
                    className={cn(
                      "flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border",
                      kind === "new" && "border-accent/60 bg-accent/15 text-accent",
                      kind === "retired" && "border-elevation-subtle-soft bg-elevation-hover text-default-primary",
                      kind === null && "border-elevation-subtle-soft",
                    )}
                  >
                    {kind === "new" && <Plus className="h-3 w-3" />}
                    {kind === "retired" && <Minus className="h-3 w-3" />}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-medium text-default-secondary">{model.id}</span>
                  <Badge
                    variant={model.origin === "offered" ? "cyan" : "outline"}
                    className={cn("shrink-0 px-1.5 py-0 content-caption font-normal", model.origin !== "offered" && "text-default-secondary")}
                  >
                    {t_i18n(ORIGIN_LABELS[model.origin])}
                  </Badge>
                  <KindToggle
                    kind="new"
                    pressed={kind === "new"}
                    disabledReason={model.origin === "offered" ? null : t_i18n("Only a model the users can pick now can be announced as new.")}
                    onToggle={() => toggle(model, "new")}
                  />
                  <KindToggle kind="retired" pressed={kind === "retired"} disabledReason={null} onToggle={() => toggle(model, "retired")} />
                </div>
              );
            })}
          </div>
        ))}
        {rows.length === 0 && !hasPanel && (
          <p className="px-3 py-6 text-center content-compact text-default-secondary">
            {directory.length === 0
              ? t_i18n("No model yet: type an id to announce it as retired.")
              : t_i18n("No model matches. Type a full id to announce it as retired.")}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-default-secondary">
        <span>
          {shown.length > ROWS_SHOWN
            ? t_i18n("Showing {shown} of {total} models: refine the search to see the others.", { shown: ROWS_SHOWN, total: shown.length })
            : t_i18n("Enter adds or removes the highlighted model, Shift+Enter retires it.")}
        </span>
        {narrowed && addable.length > 0 && addable.length <= MAX_ANNOUNCED_MODELS && (
          <Tip content={t_i18n("Models the users can pick are added as new, the others as retired.")}>
            <Button variant="ghost" size="sm" className="h-6 gap-1 px-2 text-[11px]" onClick={addShown}>
              <Plus className="h-3 w-3" /> {t_i18n("Add the {count} shown", { count: addable.length })}
            </Button>
          </Tip>
        )}
      </div>

      <ProviderCatalogsNote catalogs={catalogs} providerLabel={providerLabel} />
    </div>
  );
}

/** "New" / "Retired" on a row: pressed when the model is announced as that kind. */
function KindToggle({
  kind,
  pressed,
  disabledReason,
  onToggle,
}: {
  kind: AnnouncedKind;
  pressed: boolean;
  disabledReason: string | null;
  onToggle: () => void;
}) {
  const { t_i18n } = useFormatter();
  const button = (
    <button
      type="button"
      // The rows are reached from the search: Enter and Shift+Enter do what these do.
      tabIndex={-1}
      aria-pressed={pressed}
      disabled={!!disabledReason}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
      className={cn(
        "inline-flex h-6 shrink-0 items-center gap-1 rounded-sm border px-1.5 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        pressed && kind === "new" && "border-accent/40 bg-accent/15 text-accent",
        pressed && kind === "retired" && "border-elevation-subtle-soft bg-elevation-hover text-default-primary",
        !pressed && "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle hover:text-default-primary",
      )}
    >
      {kind === "new" ? <Plus className="h-3 w-3" /> : <Minus className="h-3 w-3" />}
      {kind === "new" ? t_i18n("New") : t_i18n("Retired")}
    </button>
  );
  if (!disabledReason) return button;
  return (
    // A click on the disabled control must not reach the row, which would add the model as the other kind.
    <span className="inline-flex shrink-0" onClick={(event) => event.stopPropagation()}>
      <Tip content={disabledReason} wrap wrapTabIndex={-1}>
        {button}
      </Tip>
    </span>
  );
}

/** How the providers answered when asked which models they serve. */
function ProviderCatalogsNote({
  catalogs,
  providerLabel,
}: {
  catalogs: ProviderCatalogsState;
  providerLabel: (provider: string) => string;
}) {
  const { t_i18n } = useFormatter();
  if (catalogs.providers === null && !catalogs.loading && !catalogs.error) return null;
  const refresh = (
    <Tip content={t_i18n("Ask the providers again")}>
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 shrink-0 p-0"
        aria-label={t_i18n("Ask the providers again")}
        disabled={catalogs.loading || catalogs.refreshing}
        onClick={catalogs.onRefresh}
      >
        <RefreshCw className={cn("h-3 w-3", catalogs.refreshing && "animate-spin")} />
      </Button>
    </Tip>
  );
  if (catalogs.loading) {
    return (
      <p className="flex items-center gap-1.5 text-[11px] text-default-secondary">
        <Spinner size="sm" tone="inherit" />
        {t_i18n("Asking your providers which models they serve...")}
      </p>
    );
  }
  if (catalogs.error) {
    return (
      <div className="flex items-start gap-1.5 content-caption text-feedback-warning-primary">
        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
        <p className="min-w-0 flex-1">{t_i18n("Your providers' model lists could not be loaded: {error}", { error: catalogs.error })}</p>
        {refresh}
      </div>
    );
  }
  const providers = catalogs.providers ?? [];
  const failed = providers.filter((p) => p.error);
  const truncated = providers.filter((p) => !p.error && p.truncated);
  const answered = providers.length - failed.length;
  const nameOf = (p: ProviderCatalogSummary) => (p.label ? `${providerLabel(p.provider)} (${p.label})` : providerLabel(p.provider));
  return (
    <div className="flex items-start gap-1.5 text-[11px] text-default-secondary">
      <div className="min-w-0 flex-1 space-y-0.5">
        <p>
          {t_i18n("{count, plural, =0 {No provider listed its models.} one {# provider listed its models.} other {# providers listed their models.}}", {
            count: answered,
          })}
        </p>
        {truncated.map((p) => (
          <p key={p.provider_id}>
            {t_i18n("{provider} serves more than {count} models: only the first {count} are listed, type any other id.", {
              provider: nameOf(p),
              count: p.model_count,
            })}
          </p>
        ))}
        {failed.map((p) => (
          <p key={p.provider_id} className="flex items-start gap-1 text-feedback-warning-primary">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <span className="min-w-0 break-words">
              {t_i18n("{provider} did not list its models: {error}", {
                provider: nameOf(p),
                error: p.error ?? "",
              })}
            </span>
          </p>
        ))}
      </div>
      {refresh}
    </div>
  );
}
