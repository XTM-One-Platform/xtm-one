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
 * Settings > Organization: Assets & exposure, Prioritization policy and
 * Enterprise knowledge (#11543 S3). The same SettingsCard and DataTable
 * pattern as the primary assets and software inventory cards: read mode
 * draws text, edit mode draws the controls, the page's Save writes them.
 */

import { type ComponentProps, type ComponentType, type ReactNode, useEffect, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter, type SortValue } from "@/components/ui/data-table";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  SettingsCard,
  type SettingsCardFold,
  SettingsField,
  SettingsFieldGrid,
  SettingsSwitchField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useEnterpriseKnowledge } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { initialTableState, type DataTableState } from "@/lib/data-table-state";
import { ASSET_CATEGORY_LABELS, ASSET_CRITICALITY_LABELS, parseTypeValue, typeLabel, typeOptions, typeValue } from "@/lib/asset-taxonomy";
import {
  type AssetGroupRow,
  type AssetRow,
  CRITICALITY_LABELS,
  type KnowledgeLink,
  type PrioritizationPolicy,
  type ProfileForm,
} from "@/lib/organization-profile";

/** @i18n-keys */
const INTERNET_FACING_LABELS = { yes: "Internet-facing", no: "Internal only", unknown: "Exposure unknown" } as const;
/** @i18n-keys */
const KNOWLEDGE_KIND_LABELS = {
  sop: "Standard operating procedure",
  runbook: "Runbook",
  patch_policy: "Patch policy",
  reference: "Reference",
  other: "Other",
} as const;

/**
 * Read-mode widths of the asset table (layout fixed), in px. The Asset name takes the rest.
 * At 1080 the card's table is 816 px: these 680 px leave the name 136 px, and every
 * column holds its header in en, fr, es and ja with the sort mark (UX r1 B1, #11781).
 * Exposure keeps the Internet-facing badge on one line in every locale and Asset group
 * holds its header in fr and es (UX r2 B1/B3, #11781); Type is cut with its tooltip.
 */
export const ASSET_READ_WIDTHS = { type: 130, criticality: 120, internet_facing: 170, owner: 120, group: 140 } as const;

/**
 * Edit-mode widths of the asset table (layout fixed too), in px. Each picker holds its longest
 * English option whole beside its chevron: Criticality "Criticality unknown", Exposure "Exposure
 * unknown", Type a category such as "Web application" (#12271 UX r3 B2: a 128 px Criticality cell
 * let the 172 px "Criticality unknown" button run past it). A longer translation or a
 * Category > Subcategory ends in an ellipsis, chevron kept, and reads whole on hover. Owner and
 * Asset group step aside below a 1152 px card (the Columns pill brings them back), so at 1080
 * the name keeps 816 - 160 - 200 - 200 - 56 (remove) = 200 px. Measured in the lab at 1080: the
 * "Criticality unknown" button is 174 px and "Exposure unknown" 172 px, each plus 24 px of cell padding.
 */
export const ASSET_EDIT_WIDTHS = { type: 160, criticality: 200, internet_facing: 200, owner: 140, group: 136 } as const;
/** The card width below which the free-text and group columns of the asset table in edit go to the Columns pill. */
const ASSET_EDIT_STEP_ASIDE = "6xl" as const;
/** The remove column of a table in edit: the 32 px RowRemoveButton and its 12 px padding. */
export const EDIT_ACTIONS_WIDTH = 56;

/** The policy the page proposes for a first save, as the backend's RECOMMENDED_POLICY. */
export const RECOMMENDED_POLICY: PrioritizationPolicy = {
  cvss_min: 7,
  epss_min: 0.1,
  kev_always_in: true,
  internet_exposure_required: false,
  min_primary_asset_criticality: "",
  exclusions: [],
  accepted_risks: [],
};

const EMPTY_POLICY: PrioritizationPolicy = {
  cvss_min: null,
  epss_min: null,
  kev_always_in: true,
  internet_exposure_required: false,
  min_primary_asset_criticality: "",
  exclusions: [],
  accepted_risks: [],
};

/** CTEM-08: the backend defaults of the PIR score thresholds (organization_context.PrioritizationPolicy). */
const PIR_SCORE_DEFAULTS = { pir_min_score_vulnerability: 70, pir_min_score_threat: 75 } as const;

/** Days before an accepted risk's expiry when the page starts warning. */
const EXPIRY_REMINDER_DAYS = 30;
const ANY_CRITICALITY = "any";
/** The "any" choice of the policy's criticality. @i18n-keys */
const ANY_CRITICALITY_LABEL = { any: "Any criticality" } as const;
const POLICY_CRITICALITY_LABELS: Record<string, string> = { ...ANY_CRITICALITY_LABEL, ...CRITICALITY_LABELS };

/** "expired", "soon" (within the reminder window) or "ok" for a yyyy-mm-dd expiry, against *today*. */
export function expiryState(expiresOn: string, today: Date = new Date()): "expired" | "soon" | "ok" | "unset" {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expiresOn);
  if (!match) return "unset";
  const day = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const days = Math.round((day.getTime() - start.getTime()) / 86_400_000);
  if (days < 0) return "expired";
  if (days <= EXPIRY_REMINDER_DAYS) return "soon";
  return "ok";
}

/** A problem of one exposure row or field the page shows inline, and that blocks Save (UX B3). */
export interface ExposureIssue {
  section: "asset" | "group" | "risk" | "score";
  /** The row in the form (rows left blank included), for an asset or a risk. */
  index?: number;
  field: string;
  /** The catalogued English text, translated where it is drawn. */
  message: string;
}

/** @i18n-keys */
const ISSUE_MESSAGES = {
  group: "Not an asset group of this list: pick a group or None.",
  owner: "A decision owner is required.",
  refused: "This value is refused: check it and save again.",
} as const;

/** What the exposure part of *form* would make the API refuse, row by row (UX B3): checked before Save. */
export function exposureIssues(form: ProfileForm): ExposureIssue[] {
  const assets = form.assets_exposure ?? [];
  const groups = new Set((form.asset_groups ?? []).filter((group) => group.name.trim()).map((group) => group.name.trim().toLowerCase()));
  const issues: ExposureIssue[] = [];
  assets.forEach((asset, index) => {
    if (asset.name.trim() && asset.group.trim() && !groups.has(asset.group.trim().toLowerCase())) {
      issues.push({ section: "asset", index, field: "group", message: ISSUE_MESSAGES.group });
    }
  });
  (form.prioritization_policy?.accepted_risks ?? []).forEach((risk, index) => {
    if (risk.subject.trim() && !risk.owner.trim()) issues.push({ section: "risk", index, field: "owner", message: ISSUE_MESSAGES.owner });
  });
  return issues;
}

/** The form row of the *n*-th row the API received (blank rows are dropped before the save). */
function formIndex<R>(rows: R[], kept: (row: R) => boolean, n: number): number | undefined {
  let seen = -1;
  for (let i = 0; i < rows.length; i += 1) {
    if (kept(rows[i]) && (seen += 1) === n) return i;
  }
  return undefined;
}

/** A 422 of the save that names an exposure row or field, as the issue drawn on it; null for any other refusal. */
export function exposureIssueFromRefusal(form: ProfileForm, detail: string): ExposureIssue | null {
  const assets = form.assets_exposure ?? [];
  const risks = form.prioritization_policy?.accepted_risks ?? [];
  const inGroup = /^asset (['"])(.*)\1 is in group /.exec(detail);
  if (inGroup) {
    const index = assets.findIndex((asset) => asset.name.trim().toLowerCase() === inGroup[2].trim().toLowerCase());
    return index < 0 ? null : { section: "asset", index, field: "group", message: ISSUE_MESSAGES.group };
  }
  const asset = /^assets_exposure\.(\d+)\.(\w+)/.exec(detail);
  if (asset) {
    const index = formIndex(assets, (row) => Boolean(row.name.trim()), Number(asset[1]));
    return index === undefined ? null : { section: "asset", index, field: asset[2], message: ISSUE_MESSAGES.refused };
  }
  const group = /^asset_groups\.(\d+)\.(\w+)/.exec(detail);
  if (group) {
    const index = formIndex(form.asset_groups ?? [], (row) => Boolean(row.name.trim()), Number(group[1]));
    return index === undefined ? null : { section: "group", index, field: group[2], message: ISSUE_MESSAGES.refused };
  }
  const risk = /^prioritization_policy\.accepted_risks\.(\d+)\.(\w+)/.exec(detail);
  if (risk) {
    const index = formIndex(risks, (row) => Boolean(row.subject.trim()), Number(risk[1]));
    return index === undefined ? null : { section: "risk", index, field: risk[2], message: ISSUE_MESSAGES.refused };
  }
  const score = /^prioritization_policy\.(cvss_min|epss_min|pir_min_score_vulnerability|pir_min_score_threat)\b/.exec(detail);
  return score ? { section: "score", field: score[1], message: ISSUE_MESSAGES.refused } : null;
}

/** A score typed as text (so "0." survives while typed), committed as a number in [0, max] or null when empty. */
function ScoreInput({
  id,
  value,
  max,
  placeholder,
  integer = false,
  onCommit,
  onValidity,
}: {
  id: string;
  value: number | null;
  max: number;
  /** Whole numbers only (a PIR score). */
  integer?: boolean;
  placeholder: string;
  onCommit: (value: number | null) => void;
  onValidity: (invalid: boolean) => void;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  // A value set from outside (the recommended policy) redraws the text; one this field typed ("0." is 0) does not.
  useEffect(() => {
    setText((prev) => {
      const typed = prev.trim() === "" ? null : Number(prev.trim());
      return typed === value ? prev : value === null ? "" : String(value);
    });
  }, [value]);
  const parsed = Number(text.trim());
  const inRange = (n: number) => Number.isFinite(n) && n >= 0 && n <= max && (!integer || Number.isInteger(n));
  const invalid = text.trim() !== "" && !inRange(parsed);
  return (
    <Input
      id={id}
      inputMode={integer ? "numeric" : "decimal"}
      aria-labelledby={settingsFieldLabelId(id)}
      aria-describedby={settingsFieldHintId(id)}
      aria-invalid={invalid ? true : undefined}
      placeholder={placeholder}
      value={text}
      onChange={(e) => {
        const next = e.target.value;
        setText(next);
        const n = Number(next.trim());
        const valid = next.trim() === "" || inRange(n);
        // An out-of-range score is shown and blocks Save (UX B4): never a silent send of the last valid one.
        onValidity(!valid);
        if (next.trim() === "") onCommit(null);
        else if (valid) onCommit(n);
      }}
    />
  );
}

const glyph = (name: ComponentProps<typeof Icon>["name"]): ComponentType<{ className?: string }> => {
  const Glyph = ({ className }: { className?: string }) => <Icon name={name} size={16} className={className} />;
  return Glyph;
};
const AssetsIcon = glyph("network");
const GroupsIcon = glyph("layers");
const PolicyIcon = glyph("shield-check");
const KnowledgeIcon = glyph("file-text");

interface Props {
  current: ProfileForm;
  editing: boolean;
  fieldId: string;
  update: (patch: Partial<ProfileForm>) => void;
  onImport: () => void;
  /** The problems drawn on their row or field (client checks and the save's refusal). */
  issues?: ExposureIssue[];
  /** Told when a score field holds an out-of-range value, so the page blocks Save. */
  onScoreValidity?: (invalid: boolean) => void;
  /** Each card's count line and folded state, by its profile field (#12256); none: the card does not fold. */
  fold?: (section: "assets_exposure" | "asset_groups" | "prioritization_policy" | "enterprise_knowledge") => SettingsCardFold;
  /** The card in edit alone, from its own Edit (#12256 slice 2); the whole form uses `editing`. */
  cardEdit?: ExposureCard | null;
  /** A card's Edit in read mode (null while any edit is open), and its Save / Cancel bar while it is the card in edit. */
  editAction?: (card: ExposureCard) => ReactNode;
  saveBar?: (card: ExposureCard) => ReactNode;
  /** The cards drawn, in their order (#12329: the exposure tab draws three, the knowledge tab one); default every card. */
  cards?: readonly ExposureSection[];
  /** A reader's view (#1540 UX r1 B2): no sentence points at an Edit the reader does not have. */
  readOnly?: boolean;
  /**
   * The PIR list of the Prioritization policy (#1889), drawn by the page that reads the OpenCTI PIRs:
   * the field in edit or its read tile, on the form the card shows.
   */
  pirList?: (editing: boolean, form: ProfileForm, update: (patch: Partial<ProfileForm>) => void) => ReactNode;
  /** The OpenCTI PIR sources and syncs under the policy (#1938), drawn by the page that owns the import dialog. */
  pirSources?: ReactNode;
}

/** The cards of this component, by their profile field. */
export const EXPOSURE_SECTIONS = ["assets_exposure", "asset_groups", "prioritization_policy", "enterprise_knowledge"] as const;
type ExposureSection = (typeof EXPOSURE_SECTIONS)[number];

/** The exposure cards that open in edit on their own (the policy thresholds too since #1540 UX r1 B2). */
export type ExposureCard = "assets_exposure" | "asset_groups" | "prioritization_policy" | "accepted_risks" | "enterprise_knowledge";

/** The page size of an exposure table in edit, as the list cards' editor (#12271 UX r1 B2). */
const EDIT_PAGE_SIZE = 25;

/**
 * An exposure table in edit (#12271 UX r1 B2): paged at 25 with its search and filters, as the list
 * cards' editor, never every row at once. The order is the saved one (no sort while editing);
 * search and filters also read each row as it was when the edit started, so a row never drops out
 * while typed in. A row added in this edit is pinned first, and the search and filters are cleared
 * so it shows on the first page.
 */
function useEditTable<V>(on: boolean, values: readonly V[]) {
  const fresh = () => initialTableState({ sort: null, pageSize: EDIT_PAGE_SIZE });
  const [state, setState] = useState<DataTableState>(fresh);
  const [base, setBase] = useState<readonly V[] | null>(null);
  const [added, setAdded] = useState(0);
  useEffect(() => {
    setState(fresh());
    setAdded(0);
    setBase(on ? values : null);
    // Only the start and the end of an edit reset it, never a keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on]);
  const firstAdded = values.length - added;
  const was = (row: { index: number; value: V }) => [row.value, ...(base && row.index < base.length && base[row.index] !== row.value ? [base[row.index]] : [])];
  return {
    /** The table props of the edit; none in read mode, where the table keeps its URL state. */
    props: on
      ? { state, onStateChange: setState, paginate: true, defaultPageSize: EDIT_PAGE_SIZE, pinFirst: (row: { index: number }) => row.index >= firstAdded }
      : {},
    /** The search fields of a row, on its value and on its value when the edit started. */
    search: (fields: (value: V) => (string | null | undefined)[]) => (row: { index: number; value: V }) => was(row).flatMap(fields),
    /** A filter that keeps a row on its value or on its value when the edit started. */
    filter: <R extends { index: number; value: V }>(filter: DataTableFilter<R>): DataTableFilter<R> =>
      on && filter.values ? { ...filter, values: (row) => was(row).flatMap((value) => filter.values!({ ...row, value })) } : filter,
    onAdd: (value: V) => {
      setAdded((n) => n + 1);
      setBase((prev) => (prev ? [...prev, value] : prev));
      setState((prev) => ({ ...prev, search: "", filters: {}, page: 0 }));
    },
    onRemove: (index: number) => {
      if (index >= firstAdded) setAdded((n) => Math.max(0, n - 1));
      setBase((prev) => (prev ? prev.filter((_, i) => i !== index) : prev));
    },
  };
}

const NO_GROUP = "none";

type AssetTableRow = { index: number; value: AssetRow };
type GroupTableRow = { index: number; value: AssetGroupRow };
type RiskRow = { index: number; value: PrioritizationPolicy["accepted_risks"][number] };
type KnowledgeRow = { index: number; value: KnowledgeLink };

const NEW_ASSET: AssetRow = {
  name: "",
  category: "generic_asset",
  subcategory: "",
  criticality: "unknown",
  environment: "",
  internet_facing: "unknown",
  owner: "",
  group: "",
  products: [],
  tags: [],
  source: "manual",
  external_id: "",
};

const NEW_GROUP: AssetGroupRow = {
  name: "",
  description: "",
  environment: "",
  internet_facing: "unknown",
  owner: "",
  products: [],
  tags: [],
  source: "manual",
  external_id: "",
};

export function OrganizationExposureCards({
  current,
  editing,
  fieldId,
  update,
  onImport,
  issues = [],
  onScoreValidity,
  fold = () => ({}),
  cardEdit = null,
  editAction = () => null,
  saveBar = () => null,
  cards = EXPOSURE_SECTIONS,
  readOnly = false,
  pirList,
  pirSources,
}: Props) {
  const { t_i18n } = useFormatter();
  // The policy thresholds are in edit with the whole form, or alone from the card's own Edit (#1540 UX r1 B2).
  const policyEditing = editing || cardEdit === "prioritization_policy";
  const [invalidScores, setInvalidScores] = useState<Record<string, boolean>>({});
  const anyInvalidScore = Object.values(invalidScores).some(Boolean);
  // Cancel drops what was typed: the next edit starts with valid scores.
  useEffect(() => {
    if (!policyEditing) setInvalidScores({});
  }, [policyEditing]);
  useEffect(() => {
    onScoreValidity?.(policyEditing && anyInvalidScore);
  }, [anyInvalidScore, policyEditing, onScoreValidity]);
  const issueOf = (section: ExposureIssue["section"], field: string, index?: number) =>
    issues.find((issue) => issue.section === section && issue.field === field && issue.index === index);
  // The error under a control in a table cell, named by the control's aria-describedby.
  const withError = (control: ReactNode, issue: ExposureIssue | undefined, errorId: string) =>
    issue ? (
      <div className="space-y-1">
        {control}
        <p id={errorId} role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n(issue.message)}
        </p>
      </div>
    ) : (
      control
    );
  // A card is in edit with the whole form, or alone from its own Edit (#12256 slice 2).
  const assetsEditing = editing || cardEdit === "assets_exposure";
  const groupsEditing = editing || cardEdit === "asset_groups";
  const risksEditing = editing || cardEdit === "accepted_risks";
  const knowledgeEditing = editing || cardEdit === "enterprise_knowledge";
  const assets = current.assets_exposure ?? [];
  const groups = current.asset_groups ?? [];
  const policy = current.prioritization_policy ?? EMPTY_POLICY;
  const knowledge = current.enterprise_knowledge ?? [];
  const knowledgeBases = useBindableKnowledgeBases();
  const linked = useEnterpriseKnowledge(!knowledgeEditing && knowledge.length > 0);
  const readable = new Map((linked.data ?? []).map((link) => [link.knowledge_base_id, link.readable]));
  const kbName = new Map((knowledgeBases.data ?? []).map((kb) => [kb.id, kb.name]));
  const assetsTable = useEditTable(assetsEditing, assets);
  const groupsTable = useEditTable(groupsEditing, groups);
  const risksTable = useEditTable(risksEditing, policy.accepted_risks);
  const knowledgeTable = useEditTable(knowledgeEditing, knowledge);
  const anchor = { className: SECTION_BAR_ANCHOR_CLASS, "data-anchor": "card" };
  const shown = (section: ExposureSection) => cards.includes(section);

  const readCell = (value: string) =>
    value.trim() ? <TruncatedText text={value} /> : <span className="content-compact text-default-secondary">{t_i18n("Not set")}</span>;
  const label = (labels: Record<string, string>, value: string) => t_i18n(labels[value] ?? value);
  const select = (labels: Record<string, string>, value: string, name: string, onChange: (value: string) => void) => (
    // The table's interactive cell is a flex row: without this box the select keeps its label's
    // width and runs past a narrow cell. Here it takes the cell, a longer label ends in an
    // ellipsis and the chevron stays (UX r3 B2).
    <div className="w-full min-w-0">
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={t_i18n(name)} className="w-full min-w-0" title={t_i18n(labels[value] ?? value)}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {Object.entries(labels).map(([key, text]) => (
            <SelectItem key={key} value={key}>
              {t_i18n(text)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
  // Read mode sorts; edit mode keeps every row in place under the cursor (as the primary assets table).
  const sortable = <R,>(on: boolean, value: (row: R) => SortValue) => (on ? undefined : value);
  const remove = (on: boolean, onClick: () => void) => (on ? <RowRemoveButton label={t_i18n("Remove")} onClick={onClick} /> : null);

  const patchAsset = (index: number, patch: Partial<AssetRow>) =>
    update({ assets_exposure: assets.map((row, i) => (i === index ? { ...row, ...patch } : row)) });
  const patchGroup = (index: number, patch: Partial<AssetGroupRow>) =>
    update({ asset_groups: groups.map((row, i) => (i === index ? { ...row, ...patch } : row)) });
  const exposureCell = (on: boolean, value: AssetRow | AssetGroupRow, onChange: (v: AssetRow["internet_facing"]) => void) =>
    on ? (
      select(INTERNET_FACING_LABELS, value.internet_facing, "Exposure", (v) => onChange(v as AssetRow["internet_facing"]))
    ) : value.internet_facing === "yes" ? (
      <Badge variant="warning">{t_i18n(INTERNET_FACING_LABELS.yes)}</Badge>
    ) : (
      readCell(label(INTERNET_FACING_LABELS, value.internet_facing))
    );
  const types = typeOptions(t_i18n);
  const patchPolicy = (patch: Partial<PrioritizationPolicy>) => update({ prioritization_policy: { ...policy, ...patch } });
  const patchRisk = (index: number, patch: Partial<RiskRow["value"]>) =>
    patchPolicy({ accepted_risks: policy.accepted_risks.map((row, i) => (i === index ? { ...row, ...patch } : row)) });
  const patchKnowledge = (index: number, patch: Partial<KnowledgeLink>) =>
    update({ enterprise_knowledge: knowledge.map((row, i) => (i === index ? { ...row, ...patch } : row)) });

  // Each table is drawn for its own card's mode: the whole form, or that card alone in edit (#12256 slice 2).
  const assetColumns = (editing: boolean): DataTableColumn<AssetTableRow>[] => [
    {
      key: "name",
      header: "Asset",
      // Read mode: the name takes what the sized columns leave (UX r1 B1, #11781).
      cell: ({ index, value }) =>
        editing ? (
          // A name longer than the field reads whole on hover (UX r3 A1).
          <Input aria-label={t_i18n("Asset")} placeholder="payments-api" title={value.name || undefined} value={value.name} onChange={(e) => patchAsset(index, { name: e.target.value })} />
        ) : (
          readCell(value.name)
        ),
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => value.name.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.name,
    },
    {
      // OpenAEV's taxonomy (#11781): one picker, a category alone or one of its subcategories.
      key: "type",
      header: "Type",
      hideable: true,
      width: (editing ? ASSET_EDIT_WIDTHS : ASSET_READ_WIDTHS).type,
      cell: ({ index, value }) =>
        editing ? (
          // A Category > Subcategory longer than the field is named whole by the DS tooltip (UX r1 B2, #11781).
          <Tip content={typeLabel(t_i18n, value.category, value.subcategory)} wrap="auto" wrapClassName="flex w-full">
            <SearchableSelect
              aria-label={t_i18n("Type")}
              searchable
              sort={false}
              options={types}
              value={typeValue(value.category, value.subcategory)}
              onValueChange={(v) => patchAsset(index, parseTypeValue(v))}
              nativeTitle={false}
              className="w-full"
            />
          </Tip>
        ) : (
          readCell(typeLabel(t_i18n, value.category, value.subcategory))
        ),
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => typeValue(value.category, value.subcategory)),
      csv: ({ value }) => typeValue(value.category, value.subcategory),
    },
    {
      key: "criticality",
      header: "Criticality",
      hideable: true,
      width: (editing ? ASSET_EDIT_WIDTHS : ASSET_READ_WIDTHS).criticality,
      cell: ({ index, value }) =>
        editing
          ? select(ASSET_CRITICALITY_LABELS, value.criticality || "unknown", "Criticality", (criticality) => patchAsset(index, { criticality }))
          : readCell(label(ASSET_CRITICALITY_LABELS, value.criticality || "unknown")),
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => -Object.keys(ASSET_CRITICALITY_LABELS).indexOf(value.criticality || "unknown")),
      firstSort: "asc",
      csv: ({ value }) => value.criticality,
    },
    {
      key: "internet_facing",
      header: "Exposure",
      hideable: true,
      width: (editing ? ASSET_EDIT_WIDTHS : ASSET_READ_WIDTHS).internet_facing,
      cell: ({ index, value }) => exposureCell(editing, value, (internet_facing) => patchAsset(index, { internet_facing })),
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => ["yes", "unknown", "no"].indexOf(value.internet_facing)),
      firstSort: "asc",
      csv: ({ value }) => value.internet_facing,
    },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      hideBelow: editing ? ASSET_EDIT_STEP_ASIDE : undefined,
      width: (editing ? ASSET_EDIT_WIDTHS : ASSET_READ_WIDTHS).owner,
      cell: ({ index, value }) =>
        editing ? <Input aria-label={t_i18n("Owner")} placeholder={t_i18n("Owner")} value={value.owner} onChange={(e) => patchAsset(index, { owner: e.target.value })} /> : readCell(value.owner),
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => value.owner.toLowerCase()),
      csv: ({ value }) => value.owner,
    },
    {
      key: "group",
      header: "Asset group",
      hideable: true,
      hideBelow: editing ? ASSET_EDIT_STEP_ASIDE : undefined,
      width: (editing ? ASSET_EDIT_WIDTHS : ASSET_READ_WIDTHS).group,
      cell: ({ index, value }) => {
        if (!editing) return readCell(value.group);
        // A group is a row of the Asset groups card (UX B2, #11781); a name no row carries stays shown, marked invalid.
        const groupNames = groups.filter((group) => group.name.trim()).map((group) => group.name.trim());
        const stale = value.group.trim() && !groupNames.some((name) => name.toLowerCase() === value.group.trim().toLowerCase()) ? [value.group] : [];
        const issue = issueOf("asset", "group", index);
        const errorId = `${fieldId}-asset-${index}-group-error`;
        return withError(
          <SearchableSelect
            aria-label={t_i18n("Asset group")}
            aria-invalid={issue ? true : undefined}
            aria-describedby={issue ? errorId : undefined}
            searchable="auto"
            pinned={[{ value: NO_GROUP, label: t_i18n("None") }]}
            options={[...groupNames, ...stale].map((name) => ({ value: name, label: name }))}
            value={value.group.trim() ? value.group : NO_GROUP}
            onValueChange={(group) => patchAsset(index, { group: group === NO_GROUP ? "" : group })}
            className="w-full"
          />,
          issue,
          errorId,
        );
      },
      interactive: true,
      sortValue: sortable<AssetTableRow>(editing, ({ value }) => value.group.toLowerCase()),
      csv: ({ value }) => value.group,
    },
  ];

  const groupColumns = (editing: boolean): DataTableColumn<GroupTableRow>[] => [
    {
      key: "name",
      header: "Asset group",
      width: editing ? undefined : 240,
      cell: ({ index, value }) =>
        editing ? (
          <Input aria-label={t_i18n("Asset group")} placeholder="DMZ" value={value.name} onChange={(e) => patchGroup(index, { name: e.target.value })} />
        ) : (
          readCell(value.name)
        ),
      interactive: true,
      sortValue: sortable<GroupTableRow>(editing, ({ value }) => value.name.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.name,
    },
    {
      key: "description",
      header: "Description",
      hideable: true,
      cell: ({ index, value }) =>
        editing ? (
          <Input aria-label={t_i18n("Description")} placeholder={t_i18n("Description")} value={value.description} onChange={(e) => patchGroup(index, { description: e.target.value })} />
        ) : (
          readCell(value.description)
        ),
      interactive: true,
      csv: ({ value }) => value.description,
    },
    {
      key: "internet_facing",
      header: "Exposure",
      hideable: true,
      headerClassName: "w-44",
      width: editing ? undefined : 160,
      cell: ({ index, value }) => exposureCell(editing, value, (internet_facing) => patchGroup(index, { internet_facing })),
      interactive: true,
      sortValue: sortable<GroupTableRow>(editing, ({ value }) => ["yes", "unknown", "no"].indexOf(value.internet_facing)),
      csv: ({ value }) => value.internet_facing,
    },
    {
      key: "members",
      header: "Assets",
      hideable: true,
      width: editing ? undefined : 100,
      cell: ({ value }) => {
        const count = assets.filter((asset) => asset.group.trim().toLowerCase() === value.name.trim().toLowerCase()).length;
        return <span className="content-compact tabular-nums">{count}</span>;
      },
      csv: ({ value }) => String(assets.filter((asset) => asset.group.trim().toLowerCase() === value.name.trim().toLowerCase()).length),
    },
  ];

  const riskColumns = (editing: boolean): DataTableColumn<RiskRow>[] => [
    {
      key: "subject",
      header: "Accepted risk",
      cell: ({ index, value }) =>
        editing ? (
          <Input aria-label={t_i18n("Accepted risk")} placeholder="CVE-2024-3094" value={value.subject} onChange={(e) => patchRisk(index, { subject: e.target.value })} />
        ) : (
          readCell(value.subject)
        ),
      interactive: true,
      sortValue: sortable<RiskRow>(editing, ({ value }) => value.subject.toLowerCase()),
      csv: ({ value }) => value.subject,
    },
    {
      key: "owner",
      header: "Decision owner",
      hideable: true,
      cell: ({ index, value }) => {
        if (!editing) return readCell(value.owner);
        const issue = issueOf("risk", "owner", index);
        const errorId = `${fieldId}-risk-${index}-owner-error`;
        return withError(
          <Input
            aria-label={t_i18n("Decision owner")}
            aria-invalid={issue ? true : undefined}
            aria-describedby={issue ? errorId : undefined}
            placeholder={t_i18n("Owner")}
            value={value.owner}
            onChange={(e) => patchRisk(index, { owner: e.target.value })}
          />,
          issue,
          errorId,
        );
      },
      interactive: true,
      sortValue: sortable<RiskRow>(editing, ({ value }) => value.owner.toLowerCase()),
      csv: ({ value }) => value.owner,
    },
    {
      key: "expires_on",
      header: "Expires on",
      hideable: true,
      // Wide enough for the date and its badge on one line at 1080 (UX B1).
      headerClassName: "w-72",
      sortValue: sortable<RiskRow>(editing, ({ value }) => value.expires_on),
      firstSort: "asc",
      csv: ({ value }) => value.expires_on,
      cell: ({ index, value }) => {
        if (editing) return <DateFilterInput aria-label={t_i18n("Expires on")} value={value.expires_on} onChange={(expires_on) => patchRisk(index, { expires_on })} />;
        const state = expiryState(value.expires_on);
        return (
          <span className="inline-flex items-center gap-2 whitespace-nowrap" data-testid="settings.organization.accepted-risk-expiry" data-state={state}>
            {value.expires_on.trim() ? <span className="shrink-0">{value.expires_on}</span> : readCell(value.expires_on)}
            {state === "expired" && (
              <Badge variant="destructive" className="shrink-0 whitespace-nowrap">
                {t_i18n("Expired")}
              </Badge>
            )}
            {state === "soon" && (
              <Badge variant="warning" className="shrink-0 whitespace-nowrap">
                {t_i18n("Expires soon")}
              </Badge>
            )}
          </span>
        );
      },
      interactive: true,
    },
  ];

  const knowledgeColumns = (editing: boolean): DataTableColumn<KnowledgeRow>[] => [
    {
      key: "knowledge_base",
      header: "Knowledge base",
      cell: ({ index, value }) =>
        editing ? (
          <SearchableSelect
            aria-label={t_i18n("Knowledge base")}
            options={(knowledgeBases.data ?? []).map((kb) => ({ value: kb.id, label: kb.name }))}
            value={value.knowledge_base_id}
            onValueChange={(id) => patchKnowledge(index, { knowledge_base_id: id, label: kbName.get(id) ?? value.label })}
            placeholder={t_i18n("Pick a knowledge base")}
          />
        ) : (
          <span className="inline-flex items-center gap-2">
            {readCell(kbName.get(value.knowledge_base_id) ?? value.label)}
            {readable.get(value.knowledge_base_id) === false && <Badge variant="outline">{t_i18n("Not readable by you")}</Badge>}
          </span>
        ),
      interactive: true,
      sortValue: sortable<KnowledgeRow>(editing, ({ value }) => (kbName.get(value.knowledge_base_id) ?? value.label).toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => kbName.get(value.knowledge_base_id) ?? value.label,
    },
    {
      key: "kind",
      header: "Kind",
      hideable: true,
      headerClassName: "w-56",
      sortValue: sortable<KnowledgeRow>(editing, ({ value }) => value.kind),
      csv: ({ value }) => value.kind,
      cell: ({ index, value }) => (editing ? select(KNOWLEDGE_KIND_LABELS, value.kind, "Kind", (kind) => patchKnowledge(index, { kind })) : readCell(label(KNOWLEDGE_KIND_LABELS, value.kind))),
      interactive: true,
    },
  ];

  const scoreField = (key: "cvss_min" | "epss_min", title: string, helper: string, info: string, max: number) => {
    const id = `${fieldId}-${key}`;
    const refused = issueOf("score", key);
    const error = invalidScores[key] ? t_i18n("Type a number from 0 to {max}, or leave it empty.", { max }) : refused ? t_i18n(refused.message) : undefined;
    return (
      <SettingsField id={id} label={title} helper={helper} info={info} error={error}>
        <ScoreInput
          id={id}
          value={policy[key]}
          max={max}
          placeholder={t_i18n("Off")}
          onCommit={(value) => patchPolicy({ [key]: value } as Partial<PrioritizationPolicy>)}
          onValidity={(invalid) => setInvalidScores((prev) => (prev[key] === invalid ? prev : { ...prev, [key]: invalid }))}
        />
      </SettingsField>
    );
  };

  // A PIR score threshold equal to its backend default never makes a policy "set"; a customized one does.
  const atDefault = (key: keyof typeof PIR_SCORE_DEFAULTS) => policy[key] == null || policy[key] === PIR_SCORE_DEFAULTS[key];
  // Only the fields this card edits count: keys the backend adds with their defaults (scope, SLA) never make a policy "set".
  const policySet =
    !atDefault("pir_min_score_vulnerability") ||
    !atDefault("pir_min_score_threat") ||
    (Object.keys(EMPTY_POLICY) as (keyof PrioritizationPolicy)[]).some((key) => JSON.stringify(policy[key] ?? EMPTY_POLICY[key]) !== JSON.stringify(EMPTY_POLICY[key]));

  /** CTEM-08: a PIR score threshold, 0 to 100; empty sends nothing and the backend default applies. */
  const pirField = (key: "pir_min_score_vulnerability" | "pir_min_score_threat", title: string, helper: string, info: string) => {
    const id = `${fieldId}-${key}`;
    const refused = issueOf("score", key);
    const error = invalidScores[key] ? t_i18n("Type a whole number from 0 to 100, or leave it empty for the default.") : refused ? t_i18n(refused.message) : undefined;
    return (
      <SettingsField id={id} label={title} helper={helper} info={info} error={error}>
        <ScoreInput
          id={id}
          value={policy[key] ?? null}
          max={100}
          integer
          placeholder={String(PIR_SCORE_DEFAULTS[key])}
          onCommit={(value) => patchPolicy({ [key]: value ?? undefined } as Partial<PrioritizationPolicy>)}
          onValidity={(invalid) => setInvalidScores((prev) => (prev[key] === invalid ? prev : { ...prev, [key]: invalid }))}
        />
      </SettingsField>
    );
  };
  const soonCount = policy.accepted_risks.filter((risk) => expiryState(risk.expires_on) !== "ok" && expiryState(risk.expires_on) !== "unset").length;

  return (
    <>
      {shown("assets_exposure") && (
      <SettingsCard
        id={`${fieldId}-assets_exposure`}
        {...anchor}
        {...fold("assets_exposure")}
        tabIndex={-1}
        data-testid="settings.organization.assets-exposure"
        icon={AssetsIcon}
        title={t_i18n("Assets and exposure")}
        description={t_i18n("What runs where, and what the internet can reach. Link assets to primary assets from the primary asset rows.")}
        action={
          <>
            {!assetsEditing && (
              <Button variant="secondary" className="gap-1.5" onClick={onImport}>
                <Icon name="upload" size={16} /> {t_i18n("Import CSV")}
              </Button>
            )}
            {editAction("assets_exposure")}
            {assetsEditing && (
              <Button
                variant="secondary"
                className="gap-1.5"
                onClick={() => {
                  assetsTable.onAdd({ ...NEW_ASSET });
                  update({ assets_exposure: [...assets, { ...NEW_ASSET }] });
                }}
              >
                <Icon name="plus" size={16} /> {t_i18n("Add an asset")}
              </Button>
            )}
          </>
        }
      >
        {assets.length === 0 ? (
          <p className="text-default-secondary">{t_i18n("No asset yet: the prioritization funnel cannot tell internet-facing findings apart.")}</p>
        ) : (
          <DataTable
            rows={assets.map((value, index) => ({ index, value }))}
            columns={assetColumns(assetsEditing)}
            layout="fixed"
            rowActionsWidth={EDIT_ACTIONS_WIDTH}
            rowKey={(row) => String(row.index)}
            searchFields={assetsTable.search((value) => [value.name, value.owner, value.group, typeLabel(t_i18n, value.category, value.subcategory)])}
            searchPlaceholder={t_i18n("Search...")}
            filters={(
              [
                {
                  key: "category",
                  label: "Type",
                  values: ({ value }) => [value.category],
                  options: Object.entries(ASSET_CATEGORY_LABELS).map(([value, text]) => ({ value, label: t_i18n(text) })),
                },
                {
                  key: "criticality",
                  label: "Criticality",
                  values: ({ value }) => [value.criticality || "unknown"],
                  options: Object.entries(ASSET_CRITICALITY_LABELS).map(([value, text]) => ({ value, label: t_i18n(text) })),
                  // Ordinal scale: unknown, then low to very high, never alphabetical (#12289).
                  ordered: true,
                },
                {
                  key: "internet_facing",
                  label: "Exposure",
                  values: ({ value }) => [value.internet_facing],
                  options: Object.entries(INTERNET_FACING_LABELS).map(([value, text]) => ({ value, label: t_i18n(text) })),
                },
              ] satisfies DataTableFilter<AssetTableRow>[]
            ).map(assetsTable.filter)}
            exportName={assetsEditing ? undefined : "assets-exposure"}
            urlKey={assetsEditing ? undefined : "oas_"}
            {...assetsTable.props}
            storageKey="settings-organization-assets-exposure"
            emptyLabel={t_i18n("No asset yet: the prioritization funnel cannot tell internet-facing findings apart.")}
            rowTestId="settings.organization.asset-row"
            rowActions={
              assetsEditing
                ? (row) =>
                    remove(true, () => {
                      assetsTable.onRemove(row.index);
                      update({ assets_exposure: assets.filter((_, i) => i !== row.index) });
                    })
                : undefined
            }
          />
        )}
        {saveBar("assets_exposure")}
      </SettingsCard>
      )}

      {shown("asset_groups") && (
      <SettingsCard
        id={`${fieldId}-asset_groups`}
        {...anchor}
        {...fold("asset_groups")}
        tabIndex={-1}
        data-testid="settings.organization.asset-groups"
        icon={GroupsIcon}
        title={t_i18n("Asset groups")}
        description={t_i18n("Groups the assets join, as OpenAEV asset groups. An asset picks its group on its row.")}
        action={
          <>
            {editAction("asset_groups")}
            {groupsEditing && (
              <Button
                variant="secondary"
                className="gap-1.5"
                onClick={() => {
                  groupsTable.onAdd({ ...NEW_GROUP });
                  update({ asset_groups: [...groups, { ...NEW_GROUP }] });
                }}
              >
                <Icon name="plus" size={16} /> {t_i18n("Add a group")}
              </Button>
            )}
          </>
        }
      >
        {groups.length === 0 ? (
          <p className="text-default-secondary">{t_i18n("No asset group yet.")}</p>
        ) : (
          <DataTable
            rows={groups.map((value, index) => ({ index, value }))}
            columns={groupColumns(groupsEditing)}
            layout={groupsEditing ? "auto" : "fixed"}
            rowKey={(row) => String(row.index)}
            searchFields={groupsTable.search((value) => [value.name, value.description])}
            searchPlaceholder={t_i18n("Search...")}
            exportName={groupsEditing ? undefined : "asset-groups"}
            urlKey={groupsEditing ? undefined : "oag_"}
            {...groupsTable.props}
            storageKey="settings-organization-asset-groups"
            emptyLabel={t_i18n("No asset group yet.")}
            rowTestId="settings.organization.asset-group-row"
            rowActions={
              groupsEditing
                ? (row) =>
                    remove(true, () => {
                      groupsTable.onRemove(row.index);
                      update({ asset_groups: groups.filter((_, i) => i !== row.index) });
                    })
                : undefined
            }
          />
        )}
        {saveBar("asset_groups")}
      </SettingsCard>
      )}

      {shown("prioritization_policy") && (
      <SettingsCard
        id={`${fieldId}-prioritization_policy`}
        {...anchor}
        {...fold("prioritization_policy")}
        tabIndex={-1}
        data-testid="settings.organization.prioritization-policy"
        icon={PolicyIcon}
        title={t_i18n("Prioritization policy")}
        description={t_i18n("The rules the CTEM funnel and its agents apply. Money thresholds stay in Risk appetite.")}
        action={
          policyEditing ? (
            !policySet && (
              <Button variant="secondary" className="gap-1.5" onClick={() => patchPolicy({ ...RECOMMENDED_POLICY, accepted_risks: policy.accepted_risks, exclusions: policy.exclusions })}>
                {t_i18n("Use the recommended policy")}
              </Button>
            )
          ) : (
            editAction("prioritization_policy")
          )
        }
      >
        {policyEditing ? (
          <div className="space-y-4">
            <SettingsFieldGrid>
              {scoreField(
                "cvss_min",
                t_i18n("CVSS minimum"),
                t_i18n("0 to 10. Empty: no CVSS threshold. Recommended: 7."),
                t_i18n("The CVSS base score of a vulnerability, from 0 to 10, one decimal. Example: 7 keeps a 7.5 and drops a 6.8. Read by the CTEM scope gate and the policy stage of the funnel. Empty: no CVSS threshold."),
                10,
              )}
              {scoreField(
                "epss_min",
                t_i18n("EPSS minimum"),
                t_i18n("0 to 1. Empty: no EPSS threshold. Recommended: 0.1."),
                t_i18n("The EPSS probability of exploitation in the next 30 days, from 0 to 1. Example: 0.1 keeps a 0.35 and drops a 0.02. Read by the CTEM scope gate and the policy stage of the funnel. Empty: no EPSS threshold."),
                1,
              )}
              {pirField(
                "pir_min_score_vulnerability",
                t_i18n("PIR score minimum: vulnerabilities"),
                t_i18n("0 to 100. A vulnerability flagged in a PIR starts a fleet run at or above this score. Default: 70."),
                t_i18n("The live OpenCTI PIR score of a vulnerability, a whole number from 0 to 100. Example: 80: a vulnerability flagged at 85 starts a run, one at 75 does not. Read by the PIR playbook fleet runs (CTEM-08 trigger rule). Empty: the default, 70."),
              )}
              {pirField(
                "pir_min_score_threat",
                t_i18n("PIR score minimum: threat activity"),
                t_i18n("0 to 100. An intrusion set, campaign or malware flagged in a PIR starts a fleet run at or above this score. Default: 75."),
                t_i18n("The live OpenCTI PIR score of an intrusion set, campaign or malware, a whole number from 0 to 100. Example: 80: a campaign flagged at 85 starts a run, one at 75 does not. Read by the PIR playbook fleet runs (CTEM-08 trigger rule). Empty: the default, 75."),
              )}
              {pirList?.(true, current, update)}
              <SettingsField
                id={`${fieldId}-min_primary_asset_criticality`}
                label={t_i18n("Minimum primary asset criticality")}
                helper={t_i18n("Keep only findings on assets linked to a primary asset at this level or above.")}
                info={t_i18n("The criticality of the primary asset a finding's asset is linked to: low, medium, high or critical. Example: High keeps a finding on a critical primary asset and drops one on a medium one. Read by the CTEM scope gate. Any: no criticality threshold.")}
              >
                {select(POLICY_CRITICALITY_LABELS, policy.min_primary_asset_criticality || ANY_CRITICALITY, "Minimum primary asset criticality", (v) =>
                  patchPolicy({ min_primary_asset_criticality: v === ANY_CRITICALITY ? "" : v }),
                )}
              </SettingsField>
            </SettingsFieldGrid>
            <SettingsSwitchField
              id={`${fieldId}-kev_always_in`}
              label={t_i18n("Known exploited vulnerabilities always in")}
              helper={t_i18n("A vulnerability in CISA KEV passes the thresholds whatever its scores.")}
              info={t_i18n("On: a vulnerability in the CISA Known Exploited Vulnerabilities catalog passes the CVSS and EPSS thresholds. Off: it is held to them like any other. Read by the CTEM scope gate.")}
              checked={policy.kev_always_in}
              onCheckedChange={(kev_always_in) => patchPolicy({ kev_always_in })}
            />
            <SettingsSwitchField
              id={`${fieldId}-internet_exposure_required`}
              label={t_i18n("Internet exposure required")}
              helper={t_i18n("Keep only findings on assets marked internet-facing. This drops the findings on user endpoints too.")}
              info={t_i18n("On: a finding passes only when its asset is marked internet-facing in the profile. Off: internet exposure is not checked. Read by the CTEM scope gate.")}
              checked={policy.internet_exposure_required}
              onCheckedChange={(internet_exposure_required) => patchPolicy({ internet_exposure_required })}
            />
          </div>
        ) : (
          <SettingsReadGrid columns={3}>
            <SettingsReadField label={t_i18n("CVSS minimum")} value={policy.cvss_min === null ? "" : String(policy.cvss_min)} mono />
            <SettingsReadField label={t_i18n("EPSS minimum")} value={policy.epss_min === null ? "" : String(policy.epss_min)} mono />
            <SettingsReadField
              label={t_i18n("PIR score minimum: vulnerabilities")}
              value={String(policy.pir_min_score_vulnerability ?? PIR_SCORE_DEFAULTS.pir_min_score_vulnerability)}
              mono
            />
            <SettingsReadField label={t_i18n("PIR score minimum: threat activity")} value={String(policy.pir_min_score_threat ?? PIR_SCORE_DEFAULTS.pir_min_score_threat)} mono />
            <SettingsReadField
              label={t_i18n("Minimum primary asset criticality")}
              value={policy.min_primary_asset_criticality ? label(CRITICALITY_LABELS, policy.min_primary_asset_criticality) : ""}
            />
            <SettingsReadField label={t_i18n("Known exploited vulnerabilities always in")} value={policy.kev_always_in ? t_i18n("Yes") : t_i18n("No")} />
            <SettingsReadField label={t_i18n("Internet exposure required")} value={policy.internet_exposure_required ? t_i18n("Yes") : t_i18n("No")} />
            {/* The PIR list takes the whole row (wide tile), so the three-column grid keeps its rows. */}
            {pirList?.(false, current, update)}
          </SettingsReadGrid>
        )}
        {!policyEditing && !policySet && (
          <p className="mt-3 content-compact text-default-secondary">
            {readOnly
              ? t_i18n("No policy yet: findings rank as before.")
              : t_i18n("No policy yet: findings rank as before. Edit to start from the recommended policy (CVSS 7, EPSS 0.1, KEV always in).")}
          </p>
        )}
        {saveBar("prioritization_policy")}
        {pirSources && <div className="mt-4">{pirSources}</div>}

        <div className="mt-4 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center content-base font-semibold text-default-primary">
              {t_i18n("Accepted risks")}
              {soonCount > 0 && (
                <Badge variant="warning" className="ml-2" data-testid="settings.organization.accepted-risks-reminder">
                  {t_i18n("{count, plural, one {# expires within 30 days or has expired} other {# expire within 30 days or have expired}}", { count: soonCount })}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2">
              {editAction("accepted_risks")}
              {risksEditing && (
                <Button
                  variant="secondary"
                  className="gap-1.5"
                  onClick={() => {
                    const risk = { subject: "", scope: "", owner: "", expires_on: "", reason: "" };
                    risksTable.onAdd(risk);
                    patchPolicy({ accepted_risks: [...policy.accepted_risks, risk] });
                  }}
                >
                  <Icon name="plus" size={16} /> {t_i18n("Add an accepted risk")}
                </Button>
              )}
            </div>
          </div>
          {policy.accepted_risks.length === 0 ? (
            <p className="text-default-secondary">{t_i18n("No accepted risk: every finding that passes the rules is ranked.")}</p>
          ) : (
            <DataTable
              rows={policy.accepted_risks.map((value, index) => ({ index, value }))}
              columns={riskColumns(risksEditing)}
              layout="auto"
              rowKey={(row) => String(row.index)}
              searchFields={risksTable.search((value) => [value.subject, value.owner, value.scope])}
              searchPlaceholder={t_i18n("Search...")}
              noFilters="subject, owner and expiry are searched and sorted; the reminder badge counts the expiring ones"
              exportName={risksEditing ? undefined : "accepted-risks"}
              urlKey={risksEditing ? undefined : "oar_"}
              {...risksTable.props}
              storageKey="settings-organization-accepted-risks"
              emptyLabel={t_i18n("No accepted risk: every finding that passes the rules is ranked.")}
              rowTestId="settings.organization.accepted-risk-row"
              rowActions={
                risksEditing
                  ? (row) =>
                      remove(true, () => {
                        risksTable.onRemove(row.index);
                        patchPolicy({ accepted_risks: policy.accepted_risks.filter((_, i) => i !== row.index) });
                      })
                  : undefined
              }
            />
          )}
          {saveBar("accepted_risks")}
        </div>
      </SettingsCard>
      )}

      {shown("enterprise_knowledge") && (
      <SettingsCard
        id={`${fieldId}-enterprise_knowledge`}
        {...anchor}
        {...fold("enterprise_knowledge")}
        tabIndex={-1}
        data-testid="settings.organization.enterprise-knowledge"
        icon={KnowledgeIcon}
        title={t_i18n("Enterprise knowledge")}
        description={t_i18n("Your procedures and reference documents. Agents search them on demand, each within the knowledge bases its user can read.")}
        action={
          <>
            {editAction("enterprise_knowledge")}
            {knowledgeEditing && (
              <Button
                variant="secondary"
                className="gap-1.5"
                onClick={() => {
                  const link: KnowledgeLink = { knowledge_base_id: "", label: "", kind: "sop" };
                  knowledgeTable.onAdd(link);
                  update({ enterprise_knowledge: [...knowledge, link] });
                }}
              >
                <Icon name="plus" size={16} /> {t_i18n("Link a knowledge base")}
              </Button>
            )}
          </>
        }
      >
        {knowledge.length === 0 ? (
          <p className="text-default-secondary">{t_i18n("No knowledge base linked: agents cannot follow your own procedures.")}</p>
        ) : (
          <DataTable
            rows={knowledge.map((value, index) => ({ index, value }))}
            columns={knowledgeColumns(knowledgeEditing)}
            layout="auto"
            rowKey={(row) => String(row.index)}
            searchFields={knowledgeTable.search((value) => [kbName.get(value.knowledge_base_id) ?? value.label])}
            searchPlaceholder={t_i18n("Search...")}
            filters={(
              [
                {
                  key: "kind",
                  label: "Kind",
                  values: ({ value }) => [value.kind],
                  options: Object.entries(KNOWLEDGE_KIND_LABELS).map(([value, text]) => ({ value, label: t_i18n(text) })),
                },
              ] satisfies DataTableFilter<KnowledgeRow>[]
            ).map(knowledgeTable.filter)}
            urlKey={knowledgeEditing ? undefined : "oek_"}
            {...knowledgeTable.props}
            storageKey="settings-organization-enterprise-knowledge"
            emptyLabel={t_i18n("No knowledge base linked: agents cannot follow your own procedures.")}
            rowTestId="settings.organization.knowledge-row"
            rowActions={
              knowledgeEditing
                ? (row) =>
                    remove(true, () => {
                      knowledgeTable.onRemove(row.index);
                      update({ enterprise_knowledge: knowledge.filter((_, i) => i !== row.index) });
                    })
                : undefined
            }
          />
        )}
        {saveBar("enterprise_knowledge")}
      </SettingsCard>
      )}
    </>
  );
}
