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
 * The Deep Investigation pack builder's editor: a pack's identity, its
 * ordered steps, and each step's sources, from what the server offers
 * (`usePackBuilderCatalog`). Every edit goes through `pack-draft`, and the
 * server validates the save; this component holds no rule of its own beyond
 * what the catalog says a kind takes.
 */
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";

import { ACTION_ICONS } from "@/components/investigation/action-icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  InvestigationPackFields,
  PackBuilderCatalog,
  PackExposedSetting,
  PackSourceSpec,
  PackStepSpec,
} from "@/types";

import {
  addSource,
  addStep,
  moveStep,
  newSource,
  pivotTargets,
  removeSource,
  removeStep,
  setExposed,
  updateSource,
  updateStep,
} from "./pack-draft";

const NONE = "__none__";

/** The settings a passive DNS source may offer at launch. @i18n-keys */
const SETTING_OFFER_LABELS = {
  pivot_scope: "Offer the pivot depth at launch",
  max_pages: "Offer the number of pages at launch",
  hostname_keywords: "Offer the host-name filter at launch",
} as const;

export function PackBuilder({
  draft,
  onChange,
  catalog,
  readOnly,
  canRecommend,
}: {
  draft: InvestigationPackFields;
  onChange: (next: InvestigationPackFields) => void;
  catalog: PackBuilderCatalog;
  readOnly: boolean;
  /** Pre-ticking a pack for others is its owner's (or an admin's) choice. */
  canRecommend: boolean;
}) {
  const { t_i18n } = useFormatter();
  const steps = draft.definition.steps;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t_i18n("The pack")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
            <div className="space-y-1.5">
              <Label htmlFor="pack-name">{t_i18n("Name")}</Label>
              <Input aria-label={t_i18n("Name")}
                id="pack-name"
                value={draft.name}
                disabled={readOnly}
                maxLength={200}
                placeholder={t_i18n("e.g. Storm-1516 news-site method")}
                onChange={(e) => onChange({ ...draft, name: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pack-budget">{t_i18n("Source budget")}</Label>
              <Input aria-label={t_i18n("Source budget")}
                id="pack-budget"
                type="number"
                min={1}
                max={catalog.limits.max_iterations}
                value={draft.max_iterations}
                disabled={readOnly}
                onChange={(e) => onChange({ ...draft, max_iterations: Number(e.target.value) || 1 })}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pack-description">{t_i18n("Description")}</Label>
            <textarea
              id="pack-description"
              value={draft.description}
              disabled={readOnly}
              maxLength={2000}
              rows={2}
              className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base"
              placeholder={t_i18n("What the method is for, as the picker shows it")}
              onChange={(e) => onChange({ ...draft, description: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t_i18n("Subjects it investigates")}</Label>
            <SubjectTypeChips
              options={catalog.subject_types}
              value={draft.subject_types}
              disabled={readOnly}
              onChange={(subject_types) => onChange({ ...draft, subject_types })}
            />
          </div>
          <div className="flex items-center gap-3">
            <Switch
              id="pack-recommended"
              checked={draft.recommended}
              disabled={readOnly || !canRecommend}
              onCheckedChange={(recommended) => onChange({ ...draft, recommended })}
            />
            <Label htmlFor="pack-recommended" className="content-base font-normal">
              {t_i18n("Pre-ticked in the picker for its subjects, for everyone who sees it")}
            </Label>
          </div>
        </CardContent>
      </Card>

      <ol className="space-y-3">
        {steps.map((step, index) => (
          <li key={step.key}>
            <StepCard
              draft={draft}
              step={step}
              index={index}
              count={steps.length}
              catalog={catalog}
              readOnly={readOnly}
              onChange={onChange}
            />
          </li>
        ))}
      </ol>

      {!readOnly && steps.length < catalog.limits.steps && (
        <Button
          variant="outline"
          className="w-full gap-1.5 border-dashed"
          onClick={() => onChange(addStep(draft, t_i18n("New step")))}
        >
          <Plus className="h-4 w-4" />
          {t_i18n("Add a step")}
        </Button>
      )}
    </div>
  );
}

function SubjectTypeChips({
  options,
  value,
  disabled,
  onChange,
}: {
  options: string[];
  value: string[];
  disabled: boolean;
  onChange: (next: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((type) => {
        const on = value.includes(type);
        return (
          <button
            key={type}
            type="button"
            role="switch"
            aria-checked={on}
            disabled={disabled}
            onClick={() => onChange(on ? value.filter((t) => t !== type) : [...value, type])}
            className={cn(
              "rounded-sm border px-2 py-0.5 content-compact transition-colors",
              on ? "border-accent/60 bg-accent/10 text-accent" : "border-elevation-subtle text-default-secondary hover:bg-elevation-hover",
            )}
          >
            {t_i18n(SUBJECT_LABELS[type as keyof typeof SUBJECT_LABELS] ?? type)}
          </button>
        );
      })}
    </div>
  );
}

/** How a subject type reads. @i18n-keys */
const SUBJECT_LABELS = {
  ip: "IP address",
  domain: "domain",
  url: "URL",
  hash: "file hash",
  cve: "CVE",
  threat_actor: "threat actor",
  malware: "malware",
  campaign: "campaign",
  domain_list: "domain list",
  unknown: "unknown",
} as const;

function StepCard({
  draft,
  step,
  index,
  count,
  catalog,
  readOnly,
  onChange,
}: {
  draft: InvestigationPackFields;
  step: PackStepSpec;
  index: number;
  count: number;
  catalog: PackBuilderCatalog;
  readOnly: boolean;
  onChange: (next: InvestigationPackFields) => void;
}) {
  const { t_i18n } = useFormatter();
  const Icon = ACTION_ICONS[step.icon ?? "target"] ?? ACTION_ICONS.target;
  const fedBy = draft.definition.steps.find(
    (other, i) => i < index && other.sources.some((s) => s.pivot_step === step.key),
  );

  return (
    <Card data-pack-step={step.key}>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <Icon className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0 flex-1 space-y-2">
            {/* One top line: the number, the name and the icon picker start at the same y (the shared Input wraps its
                field in a block that a centred row would offset against the trigger). */}
            <div className="flex items-start gap-2">
              <span className="flex h-9 items-center content-compact tabular-nums text-default-secondary">{index + 1}.</span>
              <Input
                aria-label={t_i18n("Step name")}
                value={step.label}
                disabled={readOnly}
                maxLength={120}
                className="font-medium"
                onChange={(e) => onChange(updateStep(draft, index, { label: e.target.value }))}
              />
              <Select
                value={step.icon ?? "target"}
                disabled={readOnly}
                onValueChange={(icon) => onChange(updateStep(draft, index, { icon }))}
              >
                <SelectTrigger className="w-[4.5rem]" aria-label={t_i18n("Icon")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {catalog.icons.map((name) => {
                    const Option = ACTION_ICONS[name] ?? ACTION_ICONS.target;
                    return (
                      <SelectItem key={name} value={name}>
                        <Option className="h-4 w-4" />
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
            </div>
            <Input
              aria-label={t_i18n("Step description")}
              value={step.description ?? ""}
              disabled={readOnly}
              maxLength={500}
              placeholder={t_i18n("What this step reads, in one line")}
              onChange={(e) => onChange(updateStep(draft, index, { description: e.target.value }))}
            />
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 content-compact">
              <label className="flex items-center gap-2">
                <Switch
                  checked={!!step.optional}
                  disabled={readOnly}
                  onCheckedChange={(optional) => onChange(updateStep(draft, index, { optional }))}
                />
                {t_i18n("Optional, chosen at launch")}
              </label>
              {step.optional && (
                <label className="flex items-center gap-2">
                  <Switch
                    checked={step.default_on !== false}
                    disabled={readOnly}
                    onCheckedChange={(default_on) => onChange(updateStep(draft, index, { default_on }))}
                  />
                  {t_i18n("On by default")}
                </label>
              )}
              {fedBy && (
                <Badge variant="outline" className="content-caption">
                  {t_i18n("Fed by the pivot of “{step}”", { step: fedBy.label })}
                </Badge>
              )}
            </div>
          </div>
          {!readOnly && (
            <div className="flex shrink-0 items-center gap-0.5">
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                aria-label={t_i18n("Move up")}
                disabled={index === 0}
                onClick={() => onChange(moveStep(draft, index, -1))}
              >
                <ArrowUp className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                aria-label={t_i18n("Move down")}
                disabled={index === count - 1}
                onClick={() => onChange(moveStep(draft, index, 1))}
              >
                <ArrowDown className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0 text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
                aria-label={t_i18n("Remove the step")}
                disabled={count === 1}
                onClick={() => onChange(removeStep(draft, index))}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          )}
        </div>

        <ul className="space-y-2 pl-12">
          {step.sources.map((source, sourceIndex) => (
            <li key={`${source.kind}-${sourceIndex}`}>
              <SourceRow
                draft={draft}
                stepIndex={index}
                sourceIndex={sourceIndex}
                source={source}
                catalog={catalog}
                readOnly={readOnly}
                onChange={onChange}
              />
            </li>
          ))}
          {step.sources.length === 0 && (
            <li className="content-compact italic text-default-secondary">
              {fedBy
                ? t_i18n("No source of its own: its work comes from the pivot that feeds it.")
                : t_i18n("No source yet: add one, or make an earlier step pivot into this one.")}
            </li>
          )}
          {!readOnly && step.sources.length < catalog.limits.sources_per_step && (
            <li>
              <Select
                value=""
                onValueChange={(kind) => onChange(addSource(draft, index, newSource(kind as PackSourceSpec["kind"], catalog.reference_sources[0]?.name)))}
              >
                <SelectTrigger className="w-56" aria-label={t_i18n("Add a source")}>
                  <SelectValue placeholder={t_i18n("+ Add a source")} />
                </SelectTrigger>
                <SelectContent>
                  {catalog.kinds.map((kind) => (
                    <SelectItem key={kind.kind} value={kind.kind}>
                      <span className="content-compact font-medium">{t_i18n(kind.label)}</span>
                      <span className="ml-2 content-caption text-default-secondary">{t_i18n(kind.description)}</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </li>
          )}
        </ul>
      </CardContent>
    </Card>
  );
}

function SourceRow({
  draft,
  stepIndex,
  sourceIndex,
  source,
  catalog,
  readOnly,
  onChange,
}: {
  draft: InvestigationPackFields;
  stepIndex: number;
  sourceIndex: number;
  source: PackSourceSpec;
  catalog: PackBuilderCatalog;
  readOnly: boolean;
  onChange: (next: InvestigationPackFields) => void;
}) {
  const { t_i18n } = useFormatter();
  const step = draft.definition.steps[stepIndex];
  const kind = catalog.kinds.find((k) => k.kind === source.kind);
  const patch = (p: Partial<PackSourceSpec>) => onChange(updateSource(draft, stepIndex, sourceIndex, p));

  return (
    <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="cyan" className="px-1.5 py-0 content-caption">
          {t_i18n(kind?.label ?? source.kind)}
        </Badge>
        {source.kind === "reference" && (
          <SearchableSelect
            aria-label={t_i18n("Reference source")}
            className="w-64"
            options={catalog.reference_sources.map((r) => ({ value: r.name, label: r.name }))}
            value={source.reference ?? ""}
            disabled={readOnly}
            onValueChange={(reference) => patch({ reference })}
            placeholder={t_i18n("Choose a lookup")}
          />
        )}
        {source.kind === "site_fingerprint" && (
          <Select
            value={source.fingerprint_set ?? catalog.fingerprint_sets[0]?.value ?? ""}
            disabled={readOnly}
            onValueChange={(fingerprint_set) => patch({ fingerprint_set })}
          >
            <SelectTrigger className="w-64" aria-label={t_i18n("Fingerprint set")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {catalog.fingerprint_sets.map((f) => (
                <SelectItem key={f.value} value={f.value}>
                  {t_i18n(f.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {source.kind === "knowledge_base" && <KnowledgeBaseSelect source={source} readOnly={readOnly} onPatch={patch} />}
        {source.kind === "mcp_tool" && <McpToolSelect source={source} readOnly={readOnly} onPatch={patch} />}
        {source.kind !== "reference" && (
          <Input
            aria-label={t_i18n("Source name")}
            value={source.name ?? ""}
            disabled={readOnly}
            maxLength={80}
            className="w-48"
            placeholder={t_i18n("Name (optional)")}
            onChange={(e) => patch({ name: e.target.value || null })}
          />
        )}
        {source.subject_types && source.subject_types.length > 0 && (
          <span className="content-caption text-default-secondary">
            {t_i18n("only for {types}", { types: source.subject_types.join(", ") })}
          </span>
        )}
        {!readOnly && (
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-6 w-6 p-0 text-default-secondary hover:text-feedback-error-primary"
            aria-label={t_i18n("Remove the source")}
            onClick={() => onChange(removeSource(draft, stepIndex, sourceIndex))}
          >
            <Trash2 className="h-3 w-3" />
          </Button>
        )}
      </div>

      {source.kind === "http" && (
        <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_12rem]">
          <Input
            aria-label={t_i18n("URL template")}
            value={source.url_template ?? ""}
            disabled={readOnly}
            maxLength={500}
            className="font-mono-plex"
            placeholder="https://api.example.com/lookup/{value}"
            onChange={(e) => patch({ url_template: e.target.value || null })}
          />
          <SettingSelect
            label={t_i18n("Subject as")}
            value={source.value_transform ?? NONE}
            readOnly={readOnly}
            options={[
              { value: NONE, label: t_i18n("as typed") },
              { value: "host", label: t_i18n("its host name") },
              { value: "slug", label: t_i18n("a slug (lowercase, underscores)") },
            ]}
            onChange={(v) => patch({ value_transform: v === NONE ? null : (v as "host" | "slug") })}
          />
          <p className="content-caption text-default-secondary sm:col-span-2">
            {t_i18n("Read as text, JSON or XML. Checked each time it is fetched: only public addresses, no credentials in the URL.")}
          </p>
        </div>
      )}

      {source.kind === "web_search" && (
        <textarea
          aria-label={t_i18n("Search prompt")}
          value={source.search_prompt ?? ""}
          disabled={readOnly}
          rows={2}
          maxLength={2000}
          className="mt-2 w-full rounded-sm border border-elevation-subtle bg-elevation-default px-2 py-1 content-compact"
          // The placeholder is shown literally: it is what the user types.
          placeholder={t_i18n("Leave empty for the default research prompt; otherwise place the subject with {value}", { value: "{value}" })}
          onChange={(e) => patch({ search_prompt: e.target.value || null })}
        />
      )}

      {source.kind === "passive_dns" && (
        <div className="mt-2 grid gap-2 sm:grid-cols-3">
          <SettingSelect
            label={t_i18n("Pivot into")}
            value={source.pivot_step ?? NONE}
            readOnly={readOnly}
            options={[
              { value: NONE, label: t_i18n("No pivot") },
              ...pivotTargets(draft, stepIndex).map((s) => ({ value: s.key, label: s.label })),
            ]}
            onChange={(v) => patch({ pivot_step: v === NONE ? null : v })}
          />
          <SettingSelect
            label={t_i18n("Pages of resolutions")}
            value={source.max_pages ? String(source.max_pages) : NONE}
            readOnly={readOnly}
            options={[
              { value: NONE, label: t_i18n("Default") },
              // A value saved through the API outside the usual steps stays shown.
              ...Array.from(new Set(["1", "5", "10", "25", ...(source.max_pages ? [String(source.max_pages)] : [])]))
                .sort((a, b) => Number(a) - Number(b))
                .map((n) => ({ value: n, label: n })),
            ]}
            onChange={(v) => patch({ max_pages: v === NONE ? null : Number(v) })}
          />
          <SettingSelect
            label={t_i18n("Host-name filter")}
            value={source.hostname_keywords ?? NONE}
            readOnly={readOnly}
            options={[
              { value: NONE, label: t_i18n("Every site") },
              ...catalog.hostname_keywords.map((k) => ({ value: k.value, label: t_i18n(k.label) })),
            ]}
            onChange={(v) => patch({ hostname_keywords: v === NONE ? null : v })}
          />
          <div className="space-y-1 sm:col-span-3">
            {catalog.settings
              .filter((s) => s.setting !== "pivot_scope" || answersLists(draft, step, source))
              .map((setting) => (
                <ExposeSwitch
                  key={setting.setting}
                  draft={draft}
                  setting={{
                    step: step.key,
                    source: sourceIndex,
                    setting: setting.setting,
                    default: currentValue(source, setting.setting, setting.choices[0]?.value ?? ""),
                  }}
                  label={t_i18n(SETTING_OFFER_LABELS[setting.setting])}
                  readOnly={readOnly}
                  onChange={onChange}
                />
              ))}
          </div>
        </div>
      )}
    </div>
  );
}

function answersLists(draft: InvestigationPackFields, step: PackStepSpec, source: PackSourceSpec): boolean {
  const types = source.subject_types ?? step.subject_types ?? draft.subject_types;
  return types.includes("domain_list");
}

/** The value a setting would default to, from the source as configured. */
function currentValue(source: PackSourceSpec, setting: PackExposedSetting["setting"], fallback: string): string {
  if (setting === "max_pages") return source.max_pages && [1, 5, 10, 25].includes(source.max_pages) ? String(source.max_pages) : "5";
  if (setting === "hostname_keywords") return source.hostname_keywords ?? "none";
  return source.pivot_scope ?? fallback;
}

function ExposeSwitch({
  draft,
  setting,
  label,
  readOnly,
  onChange,
}: {
  draft: InvestigationPackFields;
  setting: Omit<PackExposedSetting, "label">;
  label: string;
  readOnly: boolean;
  onChange: (next: InvestigationPackFields) => void;
}) {
  const on = (draft.definition.exposed ?? []).some(
    (e) => e.step === setting.step && e.source === setting.source && e.setting === setting.setting,
  );
  return (
    <label className="flex items-center gap-2 text-[11px] text-default-secondary">
      <Switch
        checked={on}
        disabled={readOnly}
        onCheckedChange={(next) => onChange(setExposed(draft, setting, next))}
      />
      {label}
    </label>
  );
}

function SettingSelect({
  label,
  value,
  options,
  readOnly,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  readOnly: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="space-y-1">
      <span className="content-caption text-default-secondary">{label}</span>
      <SearchableSelect
        aria-label={label}
        searchable="auto"
        sort={false}
        options={options}
        value={value}
        disabled={readOnly}
        onValueChange={onChange}
      />
    </div>
  );
}

function KnowledgeBaseSelect({
  source,
  readOnly,
  onPatch,
}: {
  source: PackSourceSpec;
  readOnly: boolean;
  onPatch: (patch: Partial<PackSourceSpec>) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: knowledgeBases } = useBindableKnowledgeBases();
  if (source.kb_slug || (source.kb_tags && source.kb_tags.length)) {
    // A corpus named by its seed slug or its tags (a duplicated built-in pack):
    // resolved for each runner among the corpora they can see.
    return (
      <span className="content-compact text-default-secondary">
        {source.kb_slug ? t_i18n("the corpus “{slug}”", { slug: source.kb_slug }) : t_i18n("corpora tagged {tags}", { tags: (source.kb_tags ?? []).join(", ") })}
      </span>
    );
  }
  const current = source.knowledge_base_ids?.[0] ?? "";
  return (
    <SearchableSelect
      aria-label={t_i18n("Knowledge base")}
      className="w-64"
      options={(knowledgeBases ?? []).map((kb) => ({ value: kb.id, label: kb.name }))}
      value={current}
      disabled={readOnly}
      onValueChange={(id) => onPatch({ knowledge_base_ids: [id] })}
      placeholder={t_i18n("Choose a knowledge base")}
    />
  );
}

function McpToolSelect({
  source,
  readOnly,
  onPatch,
}: {
  source: PackSourceSpec;
  readOnly: boolean;
  onPatch: (patch: Partial<PackSourceSpec>) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: servers } = useMCPServers();
  const designated = (servers ?? []).filter((s) => s.investigation_tool_name);
  return (
    <SearchableSelect
      aria-label={t_i18n("MCP server")}
      className="w-64"
      options={designated.map((s) => ({ value: s.id, label: s.name, description: s.investigation_tool_name ?? undefined }))}
      value={source.server_id ?? ""}
      disabled={readOnly}
      onValueChange={(server_id) => onPatch({ server_id })}
      placeholder={designated.length ? t_i18n("Choose a server") : t_i18n("No server has an investigation tool")}
    />
  );
}
