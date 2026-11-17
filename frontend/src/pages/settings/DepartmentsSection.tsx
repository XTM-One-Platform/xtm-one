// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Departments: the org units the LLM
 * analytics roll usage up to. A person belongs to many groups and to one
 * department, resolved from an ordered chain: the directory department their
 * identity provider sends (an HRIS field, read from the SSO attribute named
 * here, or imported from an HR export), then the first group rule by priority,
 * else Unassigned. The department is stored with the usage when it is written,
 * so the history never follows a move; "Apply to history" resolves every
 * stored usage again from the mapping. Administrators only.
 *
 * The page explains itself to an administrator who has never read the docs:
 * a "How does it work" panel with the three steps, every field with
 * a plain-words tooltip, every disabled button saying why, and "Create from the
 * org-unit groups" which turns the groups tagged Org unit into one department
 * and one rule each, so the common case (one group per department) is one click.
 */
import { useId, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Building2, Check, History, Pencil, Plus, Sparkles, Trash2, Upload } from "lucide-react";
import { Textarea, Spinner } from "@filigran/design-system";

import { DisclosureSummary } from "@/components/ui/disclosure";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, SettingsFieldGrid, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useGroups } from "@/hooks/api/useGroupApi";
import {
  useApplyDepartmentsToHistory,
  useDepartmentMapping,
  useDepartmentPreview,
  useImportDirectoryDepartments,
  useSaveDepartmentMapping,
} from "@/hooks/api/useInsightsApi";
import { useFormatter } from "@/i18n";
import { GROUP_KIND_LABELS } from "@/lib/group-kinds";
import { cn } from "@/lib/utils";
import type { DepartmentMapping, DepartmentSource } from "@/types";

/** The resolution orders offered, as stored, and their English label. @i18n-keys */
const ORDERS = {
  "directory,groups": "Directory department, then group rules",
  "groups,directory": "Group rules, then directory department",
  directory: "Directory department only",
  groups: "Group rules only",
} as const;

type OrderKey = keyof typeof ORDERS;

function orderKey(sources: DepartmentSource[]): OrderKey {
  const key = sources.join(",");
  return (key in ORDERS ? key : "directory,groups") as OrderKey;
}

/** `email,department` lines (a header line is skipped): what an HR export pasted reads as. */
function parseDirectory(text: string): { email: string; department: string | null }[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.split(/[,;\t]/).map((part) => part.trim()))
    .filter(([email]) => email && email.includes("@"))
    .map(([email, department]) => ({ email, department: department || null }));
}

/**
 * The label over a column of fields (one field per row): body size, the
 * required marker and the "About this field" bubble, as `SettingsField` draws
 * them; every row's control names itself with `aria-labelledby={id}`.
 */
function ColumnLabel({ id, label, info, required, className }: { id: string; label: string; info: string; required?: boolean; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className={cn("flex min-w-0 items-center gap-1.5", className)}>
      <span id={id} className="flex items-center gap-1 content-base font-medium leading-none text-default-primary">
        {label}
        {required && (
          <span className="text-feedback-error-primary" aria-label={t_i18n("Required")}>
            *
          </span>
        )}
      </span>
      <InfoTip content={<div className="max-w-xs content-compact">{info}</div>} label={t_i18n("About this field")} />
    </div>
  );
}

export function DepartmentsSection() {
  const { t_i18n, intl } = useFormatter();
  const fieldId = useId();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: mapping, isLoading } = useDepartmentMapping();
  const { data: groups } = useGroups();
  const save = useSaveDepartmentMapping();
  const importDirectory = useImportDirectoryDepartments();
  const applyToHistory = useApplyDepartmentsToHistory();
  const preview = useDepartmentPreview(!!mapping && mapping.departments.length > 0);
  const [draft, setDraft] = useState<DepartmentMapping | null>(null);
  const [directoryText, setDirectoryText] = useState("");

  const groupOptions = useMemo(
    () =>
      (groups ?? []).map((group) => ({
        value: group.id,
        label: group.name,
        description: t_i18n(GROUP_KIND_LABELS[group.kind ?? "auto"]),
      })),
    [groups, t_i18n],
  );
  const groupNames = useMemo(() => new Map((groups ?? []).map((group) => [group.id, group.name])), [groups]);

  const current = draft ?? mapping;
  const editing = draft !== null;
  const names = (current?.departments ?? []).map((department) => department.name.trim()).filter(Boolean);
  const lowerNames = new Set(names.map((name) => name.toLowerCase()));
  /** The groups tagged Org unit with no department of their name yet: what "Create from the org-unit groups" adds. */
  const orgUnitGroups = (groups ?? []).filter((group) => group.kind === "org_unit" && !lowerNames.has(group.name.trim().toLowerCase()));
  const departmentOptions = names.map((name) => ({ value: name, label: name }));
  const duplicate = new Set(names.map((name) => name.toLowerCase())).size !== names.length;
  const nameCounts = names.reduce((counts, name) => counts.set(name.toLowerCase(), (counts.get(name.toLowerCase()) ?? 0) + 1), new Map<string, number>());
  /** Why a department name cannot be saved, shown under its field while editing: empty, or the same as another one. */
  const nameError = (name: string) =>
    !name.trim()
      ? t_i18n("Enter a name.")
      : (nameCounts.get(name.trim().toLowerCase()) ?? 0) > 1
        ? t_i18n("Two departments have the same name.")
        : null;
  const departmentsHintId = `${fieldId}-departments-hint`;
  const rulesHintId = `${fieldId}-rules-hint`;
  const ruleGroupLabelId = `${fieldId}-rule-group-label`;
  const ruleDepartmentLabelId = `${fieldId}-rule-department-label`;
  const importId = `${fieldId}-import`;
  const rulesValid = (current?.rules ?? []).every((rule) => rule.group_id && names.includes(rule.department.trim()));
  const valid = !duplicate && rulesValid && names.length === (current?.departments.length ?? 0);

  const startEditing = () => {
    if (!mapping) return;
    setDraft({
      departments: mapping.departments.map((department) => ({ ...department })),
      rules: mapping.rules.map((rule) => ({ group_id: rule.group_id, department: rule.department })),
      settings: { ...mapping.settings },
    });
  };
  const update = (change: Partial<DepartmentMapping>) => draft && setDraft({ ...draft, ...change });
  const moveRule = (index: number, offset: number) => {
    if (!draft) return;
    const rules = [...draft.rules];
    const [rule] = rules.splice(index, 1);
    rules.splice(index + offset, 0, rule);
    update({ rules });
  };
  /** One department per org-unit group, named as the group, and one rule each (existing ones kept, duplicates skipped). */
  const createFromGroups = () => {
    if (!draft || !orgUnitGroups.length) return;
    const ruled = new Set(draft.rules.map((rule) => rule.group_id));
    update({
      departments: [...draft.departments, ...orgUnitGroups.map((group) => ({ name: group.name.trim(), directory_values: [] }))],
      rules: [
        ...draft.rules,
        ...orgUnitGroups.filter((group) => !ruled.has(group.id)).map((group) => ({ group_id: group.id, department: group.name.trim() })),
      ],
    });
  };
  const submit = () =>
    draft &&
    save.mutate(
      {
        ...draft,
        departments: draft.departments.map((department) => ({ ...department, name: department.name.trim() })),
      },
      {
        onSuccess: () => {
          setDraft(null);
          toast({ title: t_i18n("Departments saved"), variant: "success" });
        },
        onError: (err) =>
          toast({ title: t_i18n("The departments were not saved"), description: err.message, variant: "error" }),
      },
    );
  const runImport = () => {
    const entries = parseDirectory(directoryText);
    if (!entries.length) return;
    importDirectory.mutate(entries, {
      onSuccess: (result) => {
        setDirectoryText("");
        toast({
          title: t_i18n("{count, plural, one {# person updated} other {# people updated}}", { count: result.updated }),
          description: result.unknown_count
            ? t_i18n("{count, plural, one {# email matches nobody} other {# emails match nobody}}", { count: result.unknown_count })
            : undefined,
          variant: "success",
        });
      },
      onError: (err) => toast({ title: t_i18n("The import failed"), description: err.message, variant: "error" }),
    });
  };
  const rewriteHistory = async () => {
    const ok = await confirm({
      title: "Apply the mapping to history?",
      description: t_i18n(
        "Every stored usage is counted again for the department its person resolves to today. Department figures of past months change; nothing else does.",
      ),
      confirmLabel: "Apply to history",
    });
    if (!ok) return;
    applyToHistory.mutate(undefined, {
      onSuccess: () => toast({ title: t_i18n("The history is being resolved again"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("The history was not resolved again"), description: err.message, variant: "error" }),
    });
  };

  /**
   * The departments are the shared DataTable (data tables slice 10): in read mode search,
   * sort, CSV, the pager and URL state (`odp_`); in edit mode every row on one page,
   * unsorted, so a row never moves under the cursor while typed in. Read mode draws text
   * cut with its tooltip (a disabled input could not be read whole) and fixed widths.
   */
  type DepartmentRow = { index: number; value: DepartmentMapping["departments"][number] };
  const peopleOf = (department: DepartmentRow["value"]) =>
    preview.data?.departments.find((item) => item.department_id === department.id)?.user_count ?? 0;
  const readCell = (value: string) =>
    value.trim() ? <TruncatedText text={value} /> : <span className="content-compact text-default-secondary">{t_i18n("Not set")}</span>;
  const departmentColumns: DataTableColumn<DepartmentRow>[] = [
    {
      key: "name",
      header: "Name",
      hint: t_i18n("One line per org unit. The name is what Analytics shows; the directory values are optional."),
      // One width in both modes, so the columns never move on Edit (UX r1 B3 on #10853).
      width: 240,
      required: editing,
      interactive: editing,
      sortValue: editing ? undefined : ({ value }) => value.name.toLowerCase(),
      firstSort: "asc",
      csv: ({ value }) => value.name,
      cell: ({ index, value }) => {
        if (!editing) return readCell(value.name);
        const error = nameError(value.name);
        const errorId = `${fieldId}-name-${index}-error`;
        return (
          <div className="min-w-0 flex-1 space-y-1">
            <Input
              aria-label={t_i18n("Name")}
              aria-describedby={error ? errorId : departmentsHintId}
              aria-invalid={error ? true : undefined}
              aria-required
              value={value.name}
              onChange={(e) =>
                update({
                  departments: (current?.departments ?? []).map((item, at) => (at === index ? { ...item, name: e.target.value } : item)),
                  rules: (current?.rules ?? []).map((rule) => (rule.department === value.name ? { ...rule, department: e.target.value } : rule)),
                })
              }
            />
            {error && (
              <p id={errorId} role="alert" className="content-compact text-feedback-error-primary">
                {error}
              </p>
            )}
          </div>
        );
      },
    },
    {
      key: "directory_values",
      header: "Directory values (optional)",
      hideable: true,
      hint: t_i18n(
        "Other spellings the identity provider or an HR export writes for this department, one per entry: R&D and Research for Engineering. Press Enter after each. Leave empty when the directory writes the same name, or when you use group rules only.",
      ),
      interactive: editing,
      sortValue: editing ? undefined : ({ value }) => value.directory_values.join(", ").toLowerCase(),
      firstSort: "asc",
      csv: ({ value }) => value.directory_values.join(", "),
      cell: ({ index, value }) =>
        !editing ? (
          readCell(value.directory_values.join(", "))
        ) : (
          // The box fills its cell: every row's field shares one left and one right edge (UX r1 B3 on #10853).
          <div className="min-w-0 flex-1">
          <TagInput
            aria-label={t_i18n("Directory values (optional)")}
            aria-describedby={departmentsHintId}
            placeholder={t_i18n("Other spellings in the directory, e.g. R&D (optional)")}
            emptyLabel={t_i18n("None")}
            preserveCase
            suggestFromPlatformTags={false}
            value={value.directory_values}
            onChange={(values) =>
              update({
                departments: (current?.departments ?? []).map((item, at) => (at === index ? { ...item, directory_values: values } : item)),
              })
            }
          />
          </div>
        ),
    },
    ...(preview.data && !editing
      ? [
          {
            key: "people",
            header: "People",
            hideable: true,
            hint: t_i18n("Active people the mapping resolves to this department today."),
            // Fits "Personnes" with its sort glyph (UX r1 B4 on #10853); a longer locale is cut with the tooltip.
            width: 112,
            align: "right" as const,
            sortValue: ({ value }: DepartmentRow) => peopleOf(value),
            csv: ({ value }: DepartmentRow) => peopleOf(value),
            cell: ({ value }: DepartmentRow) => <span className="tabular-nums">{peopleOf(value)}</span>,
          },
        ]
      : []),
  ];

  const backfill = mapping?.backfill;
  return (
    <div data-testid="settings.departments">
      {ConfirmEl}
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h4 className="flex items-center gap-2 content-compact font-medium uppercase tracking-wider text-default-secondary">
            <Building2 className="h-3.5 w-3.5" /> {t_i18n("Departments")}
          </h4>
          <p className="mt-1 content-compact text-default-secondary">
            {t_i18n(
              "The org units Analytics rolls usage up to. A person belongs to many groups but counts in one department, so department figures add up to the platform total. Three steps: create the departments, say who belongs where (one rule per group is the easiest), apply the mapping to past usage.",
            )}
          </p>
        </div>
        <div className="ml-4 flex shrink-0 gap-2">
          {!editing ? (
            <Button variant="secondary" className="gap-1.5" onClick={startEditing} disabled={isLoading || !mapping}>
              <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
            </Button>
          ) : (
            <>
              <Button variant="secondary" onClick={() => setDraft(null)}>
                {t_i18n("Cancel")}
              </Button>
              <Button className="gap-1.5" disabled={save.isPending || !valid} onClick={submit} aria-busy={save.isPending || undefined}>
                {save.isPending && <Spinner size="sm" tone="inherit" />}{!save.isPending && <Check className="h-3 w-3" />}
                {t_i18n("Save")}
              </Button>
            </>
          )}
        </div>
      </div>

      {current && (
        <div className="space-y-4 rounded-sm bg-elevation-default p-3.5 content-compact">
          <details data-testid="settings.departments.help" className="rounded-sm border border-elevation-subtle p-2.5 text-default-secondary">
            <DisclosureSummary size="block" className="w-fit font-medium text-default-primary">{t_i18n("How does it work?")}</DisclosureSummary>
            <ol className="mt-2 list-decimal space-y-1 pl-5">
              <li>{t_i18n("Create one department per org unit: Engineering, Marketing, Finance. The names are yours.")}</li>
              <li>
                {t_i18n("Say who belongs where. Three sources, used in the resolution order you pick:")}
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  <li>{t_i18n("Group rules: the group \"Marketing\" counts for the department \"Marketing\". The easiest when you already have one group per team. Read top to bottom: the first group a person is a member of decides, so put the most specific group first.")}</li>
                  <li>{t_i18n("Directory department: the department your identity provider sends at sign-in (a SAML attribute or OIDC claim, usually fed by your HR system). Name the attribute, and if the directory spells a department differently (\"R&D\" for Engineering), list those spellings as the department's directory values.")}</li>
                  <li>{t_i18n("Import: paste an HR export, one line per person. For exceptions, or when no group matches your org.")}</li>
                </ul>
              </li>
              <li>{t_i18n("Save, check the preview, then apply the mapping to history so past months follow it. New usage follows it as it happens.")}</li>
            </ol>
            <p className="mt-2">{t_i18n("A person nothing resolves reads as Unassigned. Usage nobody is behind (the platform's own calls) reads as Unattributed.")}</p>
          </details>
          {!editing ? (
            // At rest (#10855): the two settings read as tiles, never as disabled fields; Edit opens the form.
            <SettingsReadGrid data-testid="settings.departments.read">
              <SettingsReadField
                label={t_i18n("Resolution order")}
                info={t_i18n("Which source wins when two of them know a person's department. With group rules only, the directory attribute and the import are ignored.")}
                value={t_i18n(ORDERS[orderKey(current.settings.sources)])}
              />
              <SettingsReadField
                label={t_i18n("Directory attribute (SAML attribute or OIDC claim)")}
                info={t_i18n(
                  "The attribute your identity provider sends at sign-in with the person's department, usually fed by your HR system. Leave it as it is when you use group rules only.",
                )}
                mono
                value={current.settings.directory_attribute.trim()}
              />
            </SettingsReadGrid>
          ) : (
          <SettingsFieldGrid>
            <SettingsField
              id={`${fieldId}-order`}
              label={t_i18n("Resolution order")}
              helper={t_i18n("Which source decides a person's department first.")}
              info={t_i18n("Which source wins when two of them know a person's department. With group rules only, the directory attribute and the import are ignored.")}
            >
              <Select
                value={orderKey(current.settings.sources)}
                onValueChange={(value) =>
                  update({ settings: { ...current.settings, sources: value.split(",") as DepartmentSource[] } })
                }
              >
                <SelectTrigger
                  id={`${fieldId}-order`}
                  aria-labelledby={settingsFieldLabelId(`${fieldId}-order`)}
                  aria-describedby={settingsFieldHintId(`${fieldId}-order`)}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(ORDERS) as OrderKey[]).map((key) => (
                    <SelectItem key={key} value={key}>
                      {t_i18n(ORDERS[key])}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </SettingsField>
            <SettingsField
              id={`${fieldId}-attribute`}
              label={t_i18n("Directory attribute (SAML attribute or OIDC claim)")}
              helper={t_i18n("The sign-in attribute that carries the department, e.g. department.")}
              info={t_i18n(
                "The attribute your identity provider sends at sign-in with the person's department, usually fed by your HR system. Leave it as it is when you use group rules only.",
              )}
            >
              <Input
                id={`${fieldId}-attribute`}
                aria-labelledby={settingsFieldLabelId(`${fieldId}-attribute`)}
                aria-describedby={settingsFieldHintId(`${fieldId}-attribute`)}
                placeholder="department"
                value={current.settings.directory_attribute}
                onChange={(e) => update({ settings: { ...current.settings, directory_attribute: e.target.value } })}
              />
            </SettingsField>
          </SettingsFieldGrid>
          )}

          <div className="space-y-2">
            <div className="font-medium">{t_i18n("Departments")}</div>
            <p id={departmentsHintId} className="content-compact text-default-secondary">
              {t_i18n("One department per org unit, its name unique. Directory values: one per entry, press Enter after each.")}
            </p>
            {current.departments.length === 0 && (
              <p className="text-default-secondary">
                {t_i18n("No department yet: everyone reads as Unassigned. Add one department per org unit, or create them from the groups tagged Org unit.")}
              </p>
            )}
            {current.departments.length > 0 && (
              <DataTable
                rows={current.departments.map((value, index) => ({ index, value }))}
                columns={departmentColumns}
                layout="fixed"
                rowKey={(row) => String(row.index)}
                searchFields={editing ? undefined : (row) => [row.value.name, ...row.value.directory_values]}
                searchPlaceholder={t_i18n("Search...")}
                noFilters="One row per department, a name and its directory spellings: the search is the only narrowing it needs."
                exportName={editing ? undefined : "departments"}
                paginate={!editing}
                urlKey={editing ? undefined : "odp_"}
                storageKey="settings-organization-departments"
                emptyLabel={t_i18n("No department yet: everyone reads as Unassigned. Add one department per org unit, or create them from the groups tagged Org unit.")}
                rowTestId="settings.departments.department-row"
                rowActions={
                  editing
                    ? (row) => (
                        <RowRemoveButton
                          label={t_i18n("Remove the department")}
                          onClick={() =>
                            update({
                              departments: current.departments.filter((_item, at) => at !== row.index),
                              rules: current.rules.filter((rule) => rule.department !== row.value.name),
                            })
                          }
                        />
                      )
                    : undefined
                }
              />
            )}
            {editing && (
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="secondary"
                  className="gap-1.5"
                  onClick={() => update({ departments: [...current.departments, { name: "", directory_values: [] }] })}
                >
                  <Plus className="h-3 w-3" /> {t_i18n("Add a department")}
                </Button>
                {orgUnitGroups.length > 0 && (
                  <Tip
                    content={t_i18n(
                      "{count, plural, one {One department and one rule from the # group tagged Org unit} other {One department and one rule from each of the # groups tagged Org unit}}: the group counts for the department of its name.",
                      { count: orgUnitGroups.length },
                    )}
                  >
                    <Button variant="secondary" className="gap-1.5" onClick={createFromGroups}>
                      <Sparkles className="h-3 w-3" /> {t_i18n("Create from the org-unit groups")}
                    </Button>
                  </Tip>
                )}
              </div>
            )}
          </div>

          <div className="space-y-2">
            <div className="font-medium">{t_i18n("Group rules, first match wins")}</div>
            <p id={rulesHintId} className="content-compact text-default-secondary">
              {t_i18n(
                "Read top to bottom: the first group a person is a member of decides their department. Put the most specific group first (Product Management above Engineering). A person in none of these groups is left to the other sources.",
              )}
            </p>
            {current.rules.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="w-6 shrink-0" aria-hidden />
                <ColumnLabel
                  id={ruleGroupLabelId}
                  className="w-64 shrink-0"
                  label={t_i18n("Group (rule column)")}
                  required={editing}
                  info={t_i18n("The group whose members this rule places. A person in several groups follows the first rule that matches.")}
                />
                <span className="invisible" aria-hidden>
                  {t_i18n("counts for")}
                </span>
                <ColumnLabel
                  id={ruleDepartmentLabelId}
                  className="w-48 shrink-0"
                  label={t_i18n("Department")}
                  required={editing}
                  info={t_i18n("The department the members of the group count for in LLM Analytics.")}
                />
              </div>
            )}
            {current.rules.length === 0 && (
              <p className="text-default-secondary">
                {t_i18n("No group rule. The easiest setup is one rule per team group: the group \"Marketing\" counts for the department \"Marketing\".")}
              </p>
            )}
            {current.rules.map((rule, index) => (
              <div key={`${rule.group_id}-${index}`} className="flex items-center gap-2">
                <span className="w-6 text-right tabular-nums text-default-secondary">{index + 1}.</span>
                {editing ? (
                  <SearchableSelect
                    aria-labelledby={ruleGroupLabelId}
                    aria-describedby={rulesHintId}
                    className="w-64"
                    options={groupOptions}
                    value={rule.group_id}
                    onValueChange={(value) =>
                      update({ rules: current.rules.map((item, at) => (at === index ? { ...item, group_id: value } : item)) })
                    }
                  />
                ) : (
                  <span className="w-64 truncate">{groupNames.get(rule.group_id) ?? rule.group_id}</span>
                )}
                <span className="text-default-secondary">{t_i18n("counts for")}</span>
                {editing ? (
                  <SearchableSelect
                    aria-labelledby={ruleDepartmentLabelId}
                    aria-describedby={rulesHintId}
                    className="w-48"
                    searchable="auto"
                    placeholder={t_i18n("Department")}
                    options={departmentOptions}
                    value={rule.department}
                    onValueChange={(value) =>
                      update({ rules: current.rules.map((item, at) => (at === index ? { ...item, department: value } : item)) })
                    }
                  />
                ) : (
                  <span className="font-medium">{rule.department}</span>
                )}
                {editing && (
                  <span className="flex gap-1">
                    <Button variant="ghost" size="sm" aria-label={t_i18n("Move up")} disabled={index === 0} onClick={() => moveRule(index, -1)}>
                      <ArrowUp className="h-3 w-3" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={t_i18n("Move down")}
                      disabled={index === current.rules.length - 1}
                      onClick={() => moveRule(index, 1)}
                    >
                      <ArrowDown className="h-3 w-3" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={t_i18n("Remove the rule")}
                      onClick={() => update({ rules: current.rules.filter((_rule, at) => at !== index) })}
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </span>
                )}
              </div>
            ))}
            {editing && (
              <Tip content={names.length ? undefined : t_i18n("Add a department first: a rule points a group at a department.")} wrap>
                <Button
                  variant="secondary"
                  className="gap-1.5"
                  disabled={!names.length}
                  onClick={() => update({ rules: [...current.rules, { group_id: "", department: names[0] ?? "" }] })}
                >
                  <Plus className="h-3 w-3" /> {t_i18n("Add a group rule")}
                </Button>
              </Tip>
            )}
          </div>

          {preview.data && !editing && (
            <div className="space-y-1">
              <div className="font-medium">{t_i18n("Preview")}</div>
              <p className="text-default-secondary">
                {t_i18n("{resolved} of {total} active people resolved, {unassigned} unassigned", {
                  resolved: preview.data.resolved_count,
                  total: preview.data.user_count,
                  unassigned: preview.data.unassigned_count,
                })}
              </p>
              {[...preview.data.departments, { department_id: "unassigned", name: t_i18n("Unassigned"), user_count: preview.data.unassigned_count, users: preview.data.unassigned }]
                .filter((item) => item.user_count > 0)
                .map((item) => (
                  <details key={item.department_id}>
                    <DisclosureSummary size="block" className="w-fit">
                      {item.name} ({item.user_count})
                    </DisclosureSummary>
                    <p className="mt-1 pl-4 text-default-secondary">
                      {item.users.map((person) => person.email).join(", ")}
                      {item.user_count > item.users.length && " ..."}
                    </p>
                  </details>
                ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="space-y-2 rounded-sm bg-elevation-default p-3.5 content-compact">
          <div className="font-medium">{t_i18n("Import directory departments")}</div>
          <SettingsField
            id={importId}
            label={t_i18n("Email and department per line")}
            helper={t_i18n(
              "Optional. Paste an HR export, one person per line: email, then department. For exceptions, or when no group matches your org; with group rules only it is ignored. A sign-in whose identity provider sends the directory attribute replaces the imported value.",
            )}
            info={t_i18n("One line per person: the sign-in email, then the department name. A header line is skipped.")}
          >
            <Textarea
              id={importId}
              aria-labelledby={settingsFieldLabelId(importId)}
              aria-describedby={settingsFieldHintId(importId)}
              placeholder="ada@example.com,Engineering"
              value={directoryText}
              onChange={(e) => setDirectoryText(e.target.value)}
              minRows={3}
              maxRows={8}
            />
          </SettingsField>
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={importDirectory.isPending || parseDirectory(directoryText).length === 0}
            onClick={runImport} aria-busy={importDirectory.isPending || undefined}>
            {importDirectory.isPending && <Spinner size="sm" tone="inherit" />}{!importDirectory.isPending && <Upload className="h-3 w-3" />}
            {t_i18n("Import")}
          </Button>
        </div>
        <div className="space-y-2 rounded-sm bg-elevation-default p-3.5 content-compact">
          <div className="font-medium">{t_i18n("History")}</div>
          <p className="text-default-secondary">
            {t_i18n("Past usage keeps the department it was stored with. After a change of the mapping, apply it to history so past months follow the new mapping.")}
          </p>
          <p className="text-default-secondary">
            {backfill?.completed_at
              ? t_i18n("Every stored usage has its person and department since {date}.", { date: intl.formatDate(new Date(backfill.completed_at), { dateStyle: "medium", timeStyle: "short" }) })
              : backfill?.cursor_date
                ? t_i18n("Stamping the stored usage with its person and department: done back to {date}.", { date: backfill.cursor_date })
                : t_i18n("The stored usage is stamped with its person and department by the next trace maintenance.")}
          </p>
          <Tip
            content={
              !mapping?.departments.length
                ? t_i18n("Save at least one department first.")
                : editing
                  ? t_i18n("Save or cancel the edit first.")
                  : undefined
            }
            wrap
          >
            <Button
              variant="secondary"
              className="gap-1.5"
              disabled={applyToHistory.isPending || !mapping?.departments.length || editing}
              onClick={rewriteHistory} aria-busy={applyToHistory.isPending || undefined}>
              {applyToHistory.isPending && <Spinner size="sm" tone="inherit" />}{!applyToHistory.isPending && <History className="h-3 w-3" />}
              {t_i18n("Apply the mapping to history")}
            </Button>
          </Tip>
        </div>
      </div>
    </div>
  );
}
